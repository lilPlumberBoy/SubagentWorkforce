# MVP Build Task Execution Overlay

Allowed work: implement only approved design packages, integrate them into a working MVP, and run the minimum validation needed to prove the feature works.

Use only the injected Resolved Inputs, owned paths, task-declared outputs, and explicit inbound handoffs as authoritative context.

Do not pull in sibling objective artifacts, contract files, or repo paths unless they were explicitly injected into the assignment inputs or resolved handoff packages.

Keep reporting and evidence lightweight. Prefer implementation tasks that emit their own validation artifacts over standalone evidence, review, or conformance tasks.

If an upstream contract or handoff is contradictory, reconcile it at the producing lane before continuing downstream implementation.

Forbidden work: unapproved scope expansion, architecture changes, or standalone reporting work that does not directly unblock implementation.
