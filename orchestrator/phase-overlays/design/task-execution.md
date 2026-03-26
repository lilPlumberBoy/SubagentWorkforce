# Design Task Execution Overlay

Allowed work: define contracts, interfaces, review gates, and approved design artifacts for the current objective using only the injected inputs and owned paths.

Use only the injected Resolved Inputs, owned paths, and current-phase approved artifacts as authoritative context.

Start by authoring the declared design artifacts. Do not spend time on exploratory shell commands such as `pwd`, `ls`, or directory probing when the task assignment already declares the output paths.

Do not re-read the generated task prompt from `runs/...` after launch unless that prompt file was explicitly injected as an input.

If this task creates the declared design artifacts itself, do not run `test -f`, `rg`, or `grep` against those same newly written files just to prove they exist or contain headings. Write the artifacts and return.

Do not mine `docs/polish` or unrelated historical outputs unless an exact path was explicitly injected into the task inputs.

Forbidden work: implementation that is not strictly required to validate a design artifact.
