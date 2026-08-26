# AI DocOps for SST: An Answerer and Gap Tracker

AI DocOps for SST is a local-first demonstration of how AI can help
[Structural Simulation Toolkit (SST)](https://sst-simulator.org/) users find
practical guidance while identifying documentation that needs attention.

The project has two currently usable parts:

- **SST Answerer** uses retrieval-augmented generation across separate SST
  documentation and source-code collections. It provides clickable citations,
  marks answers found only in source code as likely documentation gaps, and
  gives an honest not-found response when neither collection supports an
  answer.
- **Gap Tracker** collects those gap signals and user feedback, groups related
  questions, and produces maintainer-ready reports showing where documentation
  improvements would help users.

Inference is designed to run locally, including on systems where project code
cannot be sent to cloud-hosted language models.

## Project status

This repository currently contains only this project overview. The source,
installation instructions, release license, and pre-seeded container image
will be published after validation and public-release review are complete.
