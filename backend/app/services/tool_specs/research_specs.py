from __future__ import annotations

from app.services.tool_specs.base import DEFERRED_GROUPS, ToolSpec

RESEARCH_TOOL_SPECS: list[ToolSpec] = [
            ToolSpec(
                name="manage_notes",
                description=(
                    "Use this for a smaller, linkable note tied to one or more "
                    "specific projects/tasks -- a fact, decision, observation, "
                    "procedure, or preference you want future sessions to find via "
                    "semantic search. This is not manage_knowledge: manage_knowledge "
                    "is for standalone titled/categorized reference articles; "
                    "manage_notes is for notes with explicit project_id/task_id "
                    "links and a query='...' semantic search action. No status "
                    "precondition for save/search/list; link/archive need an "
                    "existing note id, from a prior save or a search/list call. If "
                    "link/archive is rejected for an unknown id, call action='list' "
                    "or action='search' first to find the right id.\n"
                    "- save: create note, pass project_id/task_id to link immediately\n"
                    "- search: semantic search (query auto-embedded) or filter by project_id/task_id\n"
                    "- link: link existing note to additional projects/tasks\n"
                    "- list: list notes, filter by project_id/task_id/note_type\n"
                    "- archive: soft-delete note\n"
                    "Notes can be linked to MULTIPLE projects and tasks simultaneously."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["save", "search", "link", "list", "archive"]},
                        "id": {"type": "string", "description": "Note id (required for link/archive)."},
                        "title": {"type": "string", "description": "Note title (required for save)."},
                        "content": {"type": "string", "description": "Note content (required for save)."},
                        "note_type": {"type": "string", "enum": ["fact", "decision", "observation", "procedure", "preference"]},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "embedding": {"type": "array", "items": {"type": "number"}, "description": "1536-dim vector for search (auto-generated if query provided)."},
                        "project_id": {"type": "string", "description": "Link note to this project (save/link) or filter by project (list/search)."},
                        "task_id": {"type": "string", "description": "Link note to this task (save/link) or filter by task (list/search)."},
                        "query": {"type": "string", "description": "Semantic search query (auto-embedded)."},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["action"],
                },
                handler="manage_notes",
                tier="deferred",
                permission="write",
                entity="agent_notes",
                slash_alias=None,
                group="research",
            ),
            ToolSpec(
                name="get_minimal_context",
                description=(
                    "Use this when you're about to touch code and want a compact, "
                    "relevant slice of the project's code graph for a natural-"
                    "language query, instead of reading whole files blind. This is "
                    "not get_impact_radius: get_impact_radius answers 'what breaks "
                    "if I change this file', get_minimal_context answers 'what code "
                    "is relevant to this topic'. Read-only, no status precondition "
                    "-- works for any project the graph has indexed. If results "
                    "look thin or empty, the graph may not be built for this repo "
                    "yet; save_project_context after scanning the repo, or narrow "
                    "the query and try again."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Code or task context to find."},
                        "limit": {"type": "integer", "default": 10, "description": "Maximum matching nodes."},
                    },
                    "required": ["query"],
                },
                handler="get_minimal_context",
                tier="deferred",
                permission="read",
                entity="research",
                slash_alias=None,
                group="research",
                required_role="executor",
            ),
            ToolSpec(
                name="get_impact_radius",
                description=(
                    "Use this before or after editing a specific file, when you "
                    "need to know what else could be affected -- it returns a "
                    "compact blast-radius summary with risk and affected-file "
                    "count for that one file. This is not get_minimal_context, "
                    "which searches by topic/query across the whole graph rather "
                    "than tracing dependents of one known file path. Read-only, no "
                    "status precondition. If the file path isn't recognized, "
                    "double-check it's project-relative (not absolute) and that "
                    "the project's code graph has been built."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Project-relative file path."},
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 1,
                            "maximum": 10,
                            "description": "Maximum dependency traversal depth.",
                        },
                    },
                    "required": ["file"],
                },
                handler="get_impact_radius",
                tier="deferred",
                permission="read",
                entity="research",
                slash_alias=None,
                group="research",
                required_role="executor",
            ),
]
