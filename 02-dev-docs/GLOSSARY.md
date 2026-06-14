# Knowledge Engine Glossary

This file is the naming contract for the product. Use these terms in code,
docs, API names, UI labels, and tests unless an older name must remain as a
compatibility alias.

## Core Runtime

**Knowledge Engine** is the portable runtime for scoped knowledge, skills,
tools, boards, retrieval, and agent execution support.

**Foundation** is the shared substrate: durable SQLite state, provider
bindings, hosted tool records, pipeline records, global metadata, policies,
traces, and other runtime infrastructure.

**Corpus** is filesystem knowledge content registered with the engine. Corpus
items can be libraries, skills, tools, prompts, schemas, kits, or samples.

**Registry** is the source-of-truth index of corpus entries and their metadata.
It is not the same thing as memory.

## Knowledge And Memory

**Library** is a domain or reference knowledge corpus. A library is usually
markdown-first and can be indexed by FTS or embeddings.

**Memory** is runtime knowledge captured or promoted by the system. Memory can
be scoped to a project, lane, board, capsule, agent, or global scope.

**Global Memory** is durable reusable knowledge intentionally promoted for
cross-project use. Local project/lane memory is never absorbed into global
memory automatically.

**Global Catalog** is safe metadata for discovery across scopes. It can point
to local objects without granting full content or original access.

**Object Card** is metadata about a retrievable object: title, summary, type,
owning source, scope, permissions, tags, and open modes. Object cards are not
the source content.

## Work Scopes

**Project** is a workspace or product boundary. Project stores stay sovereign.

**Lane** is a workstream inside a project, such as a branch, task, experiment,
agent loop, or board scope. Use "lane" for product language when "branch" is
too git-specific.

**Board** is scoped agent communication and coordination state. A board can be
shared or physically scoped to a project, lane, agent, or loop.

**Capsule** is a portable context bundle for handoff or continuation. Capsules
reference or copy selected objects; they do not merge stores by default.

## Capabilities

**Skill** is an agent-facing workflow or procedure. It may be markdown, prompts,
schemas, scripts, or a package of related instructions.

**Kit** is a packaged workflow bundle. A kit can contain libraries, skills,
schemas, prompts, and tool definitions for a repeatable workflow.

**Tool** is a callable capability. Tools can be MCP tools, hosted tools,
scripts, services, static resources, or sandbox-exposed actions.

**Hosted Tool** is a registered runtime tool backed by `tools/host.py`. It can
be a script, service proxy, or static file/directory. An empty install may have
zero hosted tools, but the hosting substrate still exists.

**Execution Adapter** is the bridge to an external or sandboxed agent engine,
for example OpenClaw/NemoClaw. It receives only the tools, skills, and context
allowed by scope policy.

## Retrieval

**Retrieval Layer** is the combined discovery/search/open surface across base
corpus search, project-docs tools, board tools, embeddings, pointer resolution,
hosted tool cards, skills, and global/local catalogs.

**Retrieval Broker** is the future unifying facade over the existing retrieval
surfaces. It should route requests to adapters without flattening sovereign
stores.

**Adapter** is a source-specific implementation behind the broker, such as
base corpus, project docs, board, hosted tools, global catalog, global memory,
or execution adapter.

**Federation** is explicit cross-scope retrieval or capability use. Federation
requires selected sources, selected scopes, policy approval, and traceability.

## Naming Rules

- Prefer product terms in UI: project, lane, board, library, kit, skill, tool,
  memory, catalog.
- Keep implementation terms in code where precise: `project_docs`, `agent_board`,
  `foundation`, `registry`, `adapter`.
- Do not use "library", "memory", "catalog", and "registry" interchangeably.
- Do not use "tool" when the object is only documentation for a workflow; call
  that a skill or kit.
- Do not call local scoped memory "global" unless it has been explicitly
  promoted.
- Treat OpenClaw/NemoClaw as an execution adapter, not as the knowledge store.
