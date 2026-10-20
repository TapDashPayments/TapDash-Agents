## What and why

<!-- One or two sentences. Link the Linear issue if there is one. -->

## Docs (required)

- [ ] I updated every doc this change affects: README, architecture diagrams, runbook, setup guides
      (the `Docs updated with code` check lists the ones the map requires; `.github/docs-map.json`)
- [ ] If a component, arrow, port, file path, state or threshold changed, the matching diagram or table in
      `durable-park/docs/architecture.md` changed too
- [ ] New files are added to `.github/docs-map.json` (the check fails otherwise)

## Tests

- [ ] `cd durable-park && pytest -q -W error` passes
- [ ] Tested on the test VM, if it changes runtime behaviour (say how)

## Deploy and rollback

<!-- What has to happen on the VM (restart the gateway? copy to /opt? an admin's root step?) and how to undo it. -->
