
# --------- Orchestrator State ---------
class OrchestratorState(TypedDict, total=False):
    # inputs
    target_role: str
    background: str

    # from skill gaps
    skill_gaps_md: str

    # from trends
    trend_snippets: List[str]
    trends_md: str
    trace: List[Dict[str, Any]]

    # from books
    extracted_skills: List[str]
    books_md: str

    # final
    roadmap_md: str

    # HITL
    approved: bool
    feedback: str
    preview_md: str

    # Rework queue
    rework_targets: List[str]  # values from {"skill_gaps","trends","books"}



# --------- Subgraph wrappers ---------
def run_skill_gaps(state: OrchestratorState) -> OrchestratorState:
    app = build_skill_gaps_graph()
    sub_in = {
        "target_role": state["target_role"],
        "background": state.get("background", ""),
    }
    sub_out = app.invoke(sub_in)
    state["skill_gaps_md"] = sub_out.get("skill_gaps_md", "")
    return state



def run_trends(state: OrchestratorState) -> OrchestratorState:
    app = build_trends_subgraph()
    sub_in = {"target_role": state["target_role"]}
    sub_out = app.invoke(sub_in)
    state["trend_snippets"] = sub_out.get("trend_snippets", [])
    state["trends_md"] = sub_out.get("trends_md", "")
    if "trace" in sub_out:
        state["trace"] = sub_out["trace"]
    return state



def run_books(state: OrchestratorState) -> OrchestratorState:
    app = build_books_subgraph()
    sub_in = {"skill_gaps_md": state.get("skill_gaps_md", "")}
    sub_out = app.invoke(sub_in)
    state["extracted_skills"] = sub_out.get("extracted_skills", [])
    state["books_md"] = sub_out.get("books_md", "")
    return state



# --------- Roadmap node ---------
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
llm = ChatOpenAI(model=MODEL)  # omit temperature (some deployments only allow default)

ROADMAP_SYSTEM = """You design simple 6-week learning roadmaps.
Produce a markdown table with columns: Week | Focus | Resources | Practice | Outcome.
Be concrete and realistic. Do not include internal reasoning."""
ROADMAP_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", ROADMAP_SYSTEM),
        ("user",
         "Target role: {role}\n\nKey skill gaps:\n{gaps}\n\nTrends:\n{trends}\n\nBooks:\n{books}\n"
         "Now produce the 6-week roadmap.")
    ]
)



def build_roadmap(state: OrchestratorState) -> OrchestratorState:
    fb = state.get("feedback", "").strip()
    fb_text = f"\n\nHuman feedback to incorporate:\n{fb}\n" if fb else ""
    msgs = ROADMAP_PROMPT.format_messages(
        role=state["target_role"],
        gaps=(state.get("skill_gaps_md", "")[:2000] + fb_text),
        trends=state.get("trends_md", "")[:2000],
        books=state.get("books_md", "")[:1500],
    )
    resp = llm.invoke(msgs)
    state["roadmap_md"] = resp.content
    return state



# --------- HITL Approval Gate ---------
def approval_gate(state: OrchestratorState) -> OrchestratorState:
    """
    If 'approved' is not True, assemble a preview and stop (END).
    On the next run (with approved=True and optional feedback), router will send to build_roadmap.
    """
    if state.get("approved") is True:
        return state
    parts = [
        "# Preview",
        "## Skill Gaps", state.get("skill_gaps_md", "")[:1500],
        "## Trends", state.get("trends_md", "")[:1200],
        "## Books", state.get("books_md", "")[:1000],
    ]
    state["preview_md"] = "\n\n".join(parts)
    return state


# --------- Rework helpers ---------
def analyze_feedback(state: OrchestratorState) -> OrchestratorState:
    """
    Read feedback and decide which sections to rework.
    Heuristic: look for keywords mapped to skill_gaps, trends, books.
    """
    text = (state.get("feedback") or "").lower()

    
    def hit(words: List[str]) -> bool:
        return any(w in text for w in words)

    targets: List[str] = []
    if hit(["gap", "missing", "prereq", "foundation", "background", "competency"]):
        targets.append("skill_gaps")
    if hit(["trend", "tool", "platform", "demand", "market", "outdated", "source", "job", "report", "news", "salary"]):
        targets.append("trends")
    if hit(["book", "reading", "resource", "course", "syllabus", "textbook"]):
        targets.append("books")

    
    # Default refresh if no keywords matched
    if not targets:
        targets = ["trends"]

    # keep order & uniqueness
    seen, ordered = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    state["rework_targets"] = ordered
    return state


def rework_dispatch_decision(state: OrchestratorState) -> str:
    queue = state.get("rework_targets") or []
    if not queue:
        return "approval_gate"
    head = queue[0]

    if head == "skill_gaps":
        return "run_skill_gaps"
    if head == "trends":
        return "run_trends"
    if head == "books":
        return "run_books"
    return "approval_gate"


def pop_rework(state: OrchestratorState) -> OrchestratorState:
    queue = list(state.get("rework_targets") or [])
    if queue:
        queue.pop(0)
    state["rework_targets"] = queue
    return state


def router_decision(state: OrchestratorState) -> str:
    # Initial build-up
    if not state.get("skill_gaps_md"):
        return "run_skill_gaps"
    if not state.get("trends_md"):
        return "run_trends"
    if not state.get("books_md"):
        return "run_books"
    # Rework path if feedback exists and not approved
    if state.get("approved") is not True and (state.get("feedback") or "").strip():
        return "analyze_feedback
    # Before building the roadmap, require human approval
    if state.get("approved") is True:
        return "build_roadmap"
    return "approval_gate"



# --------- Graph ---------
def build_orchestrator_graph():
    g = StateGraph(OrchestratorState)

    # nodes
    g.add_node("router", lambda s: s)  # identity; decision via conditional edges
    g.add_node("run_skill_gaps", run_skill_gaps)
    g.add_node("run_trends", run_trends)
    g.add_node("run_books", run_books)
    g.add_node("approval_gate", approval_gate)
    g.add_node("build_roadmap", build_roadmap)

    # rework nodes
    g.add_node("analyze_feedback", analyze_feedback)
    g.add_node("rework_dispatch", lambda s: s)  # identity; conditional dispatch
    g.add_node("pop_rework", pop_rework)

    # start → router
    g.add_edge(START, "router")

    # conditional routing (main)
    g.add_conditional_edges(
        "router",
        router_decision,
        {
            "run_skill_gaps": "run_skill_gaps",
            "run_trends": "run_trends",
            "run_books": "run_books",
            "analyze_feedback": "analyze_feedback",
            "approval_gate": "approval_gate",
            "build_roadmap": "build_roadmap",
        },
    )

    # rework flow
    g.add_edge("analyze_feedback", "rework_dispatch")
    g.add_conditional_edges(
        "rework_dispatch",
        rework_dispatch_decision,
        {
            "run_skill_gaps": "run_skill_gaps",
            "run_trends": "run_trends",
            "run_books": "run_books",
            "approval_gate": "approval_gate",
        },
    )

    # After each task, go either back to router (normal) or pop_rework (rework mode)
    def next_after_task(state: OrchestratorState) -> str:
        return "pop_rework" if (state.get("rework_targets") or []) else "router"

    g.add_conditional_edges(
        "run_skill_gaps",
        next_after_task,
        {"router": "router", "pop_rework": "pop_rework"},
    )
    g.add_conditional_edges(
        "run_trends",
        next_after_task,
        {"router": "router", "pop_rework": "pop_rework"},
    )
    g.add_conditional_edges(
        "run_books",
        next_after_task,
        {"router": "router", "pop_rework": "pop_rework"},
    )
    g.add_edge("pop_rework", "rework_dispatch")

    # Stop after preview (await human), or after final roadmap
    g.add_edge("approval_gate", END)
    g.add_edge("build_roadmap", END)

    return g.compile()



# --------- Demo runner ---------
if __name__ == "__main__":
    app = build_orchestrator_graph()

    # PASS 1: Build sections → preview → stop
    s1 = app.invoke({
        "target_role": "Data Analyst",
        "background": "Junior accountant, strong Excel; beginner SQL; no Python",
    })
    if s1.get("preview_md"):
        print("\n=== PREVIEW (Human Review) ===\n")
        print(s1["preview_md"])
    else:
        print("\nNo preview; already approved?")

    # Simulate HUMAN saying "no" with feedback
    feedback = "Outdated tools in trends; add more Python and SQL resources in books."
    s2 = dict(s1, approved=False, feedback=feedback)

    # PASS 2: Rework loop → new preview
    s3 = app.invoke(s2)
    print("\n=== PREVIEW AFTER REWORK ===\n")
    print(s3.get("preview_md", "(no preview)"))

    # PASS 3: Approve to finalize
    s4 = app.invoke(dict(s3, approved=True))
    print("\n=== ROADMAP (Final) ===\n")
    print(s4.get("roadmap_md", ""))
    

