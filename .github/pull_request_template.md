## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] Tests pass (`go test ./tools/...`, eventing/dom-synth unittests — see CONTRIBUTING.md)
- [ ] `make render` leaves no diff (renderer outputs are generated, not hand-edited)
- [ ] Shell changes pass `shellcheck`
- [ ] Docs updated if a make target, URL, or setup step changed
- [ ] Tried it on a live cluster (`make up` / `make ready`), or explained why not
