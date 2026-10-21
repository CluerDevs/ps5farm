# Setting up a console for ps5farm

This is a field guide, written from one working setup. It points at the upstream projects
instead of redistributing anything. Versions will drift: always check each project's own
README, and verify downloads against the checksums or digests the release publishes.

You need a PS5 on a firmware supported by the jailbreak you use (the setup this was tested on:
fw 10.x, supported range 7.00 to 13.60 for the projects below).

## 1. Network

Use an isolated router (no internet uplink) with the PCs and consoles on it:

- Give the host PC a static address outside the router's DHCP pool (for example pool starts at
  `.10`, PC is `.2`). Leave the gateway blank on that adapter if the PC also uses Wi-Fi for internet,
  so the router doesn't become the default route.
- Give each console a manual address and set its **primary DNS to the host PC** (needed by the
  browser exploit host below).
- All of these services are unauthenticated. Keep the network isolated.

## 2. Jailbreak host

[Relapse Exploit Plus](https://github.com/samtheruby/Relapse-Exploit-Plus) ships a host binary
that serves the exploit page over a local DNS and HTTPS redirect. Run it on the PC with its DNS
mode, open the PS5's **User's Guide**, wait for "Cache verified", then start the jailbreak.

On Windows, UDP port 53 may already be taken by Internet Connection Sharing
(`SharedAccess`); stop that service while the host runs.

The jailbreak is not persistent. After every console reboot, repeat it.

## 3. Payloads

From the host's Payload Manager page, load **etaHEN**, **ShadowMountPlus** and **kstuff**. The
loader (elfldr) listens on port 9021. For file transfer also run
[ftpsrv](https://github.com/ps5-payload-dev/ftpsrv): send `ftpsrv-ps5.elf` to port 9021 and it
serves FTP on 2121 (anonymous).

## 4. PS5LM

1. Download `ps5lm-app-vX.Y.Z.zip` from the [PS5LM releases](https://github.com/cobanov/PS5LM/releases)
   and check its hash against the digest on the release page.
2. FTP the `PPSA99575` folder to `/data/homebrew/` on the console. ShadowMountPlus adds a tile.
3. Put a `.gguf` model in `/data/PS5LM/models/`. Check the project's `docs/MODELS.md` for what
   fits in the roughly 11 GiB model budget. Download models on a PC; an isolated console
   cannot use the in-app downloader (it leaves a 0-byte `.part` file and a "paused" entry in
   `/data/PS5LM/downloads.json`: delete both if that happens).
4. Open PS5LM, pick the model under **Models**, load it and wait for "Ready".

Check it from the PC: `curl http://<console>:8081/health` should answer `{"status":"ok"}` and
`curl http://<console>:8081/tools` should list the file tools.

## Quirks worth knowing

- ftpsrv has no `NLST` (use `LIST`) and replies `226` to deletes, which Python's `ftplib` treats
  as an error. `ps5farm` handles both.
- Reading `eboot.bin` back over FTP returns a decrypted ELF, not the SELF you uploaded.
- The app picks the first model it finds when it starts. Choose the one you want in **Models**.
- The model's file tools use the app's working folder, `/data/PS5LM/scratch`. It is not
  configurable in the app we tested.
