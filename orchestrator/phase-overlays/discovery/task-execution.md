# Discovery Task Execution Overlay

Allowed work: identify boundaries, unknowns, dependencies, and candidate team structures using only the injected inputs, owned paths, and declared outputs.

Use only the injected Resolved Inputs, owned paths, and task-declared outputs as authoritative context.

Start by authoring the declared discovery artifacts. Do not spend time on exploratory shell commands such as `pwd`, `ls`, or directory probing when the task assignment already declares the output paths.

Do not re-read the generated task prompt from `runs/...` after launch unless that prompt file was explicitly injected as an input.

If this task creates the declared discovery artifacts itself, do not run `test -f`, `rg`, or `grep` against those same newly written files just to prove they exist or contain headings. Write the artifacts and return.

Do not mine `docs/design`, `docs/mvp-build`, `docs/polish`, or unrelated historical artifacts unless an exact path was explicitly injected into the task inputs.

Forbidden work: detailed design, implementation, optimization, and scope growth beyond the declared discovery assignment.
