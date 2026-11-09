# Published apt repository for matrix-overlay

This branch is the **generated release tree** served by GitHub Pages; the source
lives on [`main`](../../tree/main). Site root = repository root, so the Release
file is at `dists/stable/Release`.

Add it on a Debian/Ubuntu/COSMIC client:

```sh
sudo install -d -m 755 /etc/apt/keyrings
sudo curl -fsSLo /etc/apt/keyrings/matrix-overlay.gpg \
    https://foomip.github.io/matrix-overlay/keyring/matrix-overlay.gpg
printf 'deb [signed-by=/etc/apt/keyrings/matrix-overlay.gpg arch=amd64] https://foomip.github.io/matrix-overlay stable main\n' \
    | sudo tee /etc/apt/sources.list.d/matrix-overlay.list
sudo apt-get update
apt-cache policy matrix-overlay
```

`dists/stable/main/dep11/` carries the AppStream (DEP-11) catalogue and icon sets,
which is what makes the app appear - installable, not just describable - in COSMIC
Store. Nothing in this tree is hand-edited: it is produced by
[`packaging/publish_repo.sh`](../../blob/main/packaging/publish_repo.sh) on `main`
(the rules that decide store visibility are in
[`docs/packaging-and-store.md`](../../blob/main/docs/packaging-and-store.md)).

Signing key `1552C6EFF1B7B8A7B79C405937ED4872956B954C` (ed25519, sign-only). The
private half and the revocation certificate are **not** in this repository and must
be backed up off the publishing machine. Never commit `.gnupg/` or `revocation/`.
