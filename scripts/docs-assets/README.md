# Documentation assets

What produces the images in `docs/assets/`. Regenerate one when the output it
shows changes.

## Terminal recordings

The GIFs are recorded with [VHS](https://github.com/charmbracelet/vhs), which
needs `ttyd` and `ffmpeg`. Each tape writes its GIF to `docs/assets/`. Run
them from the repository root, with `privyx` on `PATH`:

```bash
vhs scripts/docs-assets/demo.tape      # mask and unmask a prompt
vhs scripts/docs-assets/detect.tape    # privyx detect on a log
vhs scripts/docs-assets/terms.tape     # a word list
vhs scripts/docs-assets/proxy.tape     # a request through the proxy
vhs scripts/docs-assets/audit.tape     # privyx audit stats
```

`proxy.tape` and `audit.tape` start Privyx on port 8000 in front of
`stub_upstream.py` on port 9000, so both ports must be free, and they need
`curl` and `jq`.

The tapes build their sample files while recording and write every fake key
in two parts. Keep it that way: this repository holds no key-shaped string,
so that secret scanners stay quiet.

## Link-preview card

`social-card.html` is the source of `docs/assets/social-card.png`, the image
chat apps and social networks show for a link to the documentation. The
command to render it is at the top of the file. Upload the same image as the
repository's social preview under Settings → General.

## Logo and diagram

`docs/assets/logo.svg`, `logo-mark.svg`, and `how-it-works.svg` are written by
hand. `favicon.png` is `logo.svg` rendered at 96×96.
