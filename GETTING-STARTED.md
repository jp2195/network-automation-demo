# Getting Started — a step-by-step guide for newcomers

This guide assumes **you have never used a command line, Docker, or Kubernetes
before**. It walks you through everything: what to install, how to start the
demo, how to look around, and how to shut it down. Take it one section at a
time — you don't need to understand every word to get it running.

If you're already comfortable with Docker, Kubernetes, and a terminal, you
don't need this file — the [README](README.md) has the short version.

> ### ⚠️ Honest expectations first
>
> This is an **advanced networking lab**. It runs a miniature data-center's
> worth of software on your laptop — a small Kubernetes cluster, a dozen
> simulated routers, a monitoring stack, and an automation pipeline. That's
> genuinely a lot.
>
> - It needs a **powerful computer**: ideally **32 GB of RAM** and about
>   **30 GB of free disk space**. It can limp along on 16 GB but may be slow.
> - The first start **downloads several gigabytes** and takes **10–20 minutes**.
> - Some steps may still feel hard even with this guide. That's normal. Go
>   slowly, and use the troubleshooting section at the bottom.
>
> You will **not** break your computer by trying. Everything this demo creates
> lives inside Docker and is deleted cleanly with one command (`make down`).

---

## Part 0 — Absolute basics

**What is a "terminal" / "command line"?**
It's a text window where you type commands instead of clicking buttons. You
type a line, press **Enter**, and the computer does it. This whole demo is
driven by a handful of typed commands.

**How to open a terminal:**

- **macOS:** Press `Cmd` + `Space`, type `Terminal`, press **Enter**.
- **Windows:** We'll install something called **WSL** (explained below) and
  use its **Ubuntu** terminal. For now, know that you'll open it from the
  Start menu by typing `Ubuntu`.
- **Linux:** You already know. Open your terminal app.

**How to use a command:**
When this guide shows a box like this:

```bash
make status
```

…it means: click in the terminal window, type that text exactly, and press
**Enter**. You can copy-paste — select the text, copy it, then paste into the
terminal (on most terminals, paste is `Cmd+V` on Mac, `Ctrl+Shift+V` on Linux,
right-click in Ubuntu/WSL).

**How do I know a command finished?**
When the command is done, the terminal prints a fresh empty line with your
"prompt" (usually ending in `$`) and waits for the next command. Some commands
here take **many minutes** — if it looks stuck but hasn't returned to the
prompt, it's probably still working. Be patient.

---

## Part 1 — What your computer needs

| Need | Why | Minimum |
|---|---|---|
| RAM (memory) | Runs ~12 simulated routers + a monitoring stack | 16 GB (32 GB strongly recommended) |
| Free disk space | Downloaded software images | ~30 GB |
| Operating system | — | macOS, Windows 10/11, or Linux |
| Internet | First run downloads several GB | A decent connection |

You'll install a small set of free tools (next section). Don't worry about
what each one is yet — there's a one-line explanation beside each.

---

## Part 2 — Install the tools (pick your operating system)

You need these five tools. The per-OS instructions below install all of them.

| Tool | What it is, in one line |
|---|---|
| **Docker** | Runs software in isolated "containers" — the foundation everything else sits on. |
| **k3d** | Starts a tiny Kubernetes cluster (a software system that runs and manages containers) inside Docker. |
| **kubectl** | The command you use to talk to that Kubernetes cluster. |
| **helm** | A tool for installing pre-packaged software onto Kubernetes. |
| **make** | Runs the project's shortcut commands (like `make up`). |

(There's an optional sixth tool, **Go**, needed only if you want to *change*
the network design. You can skip it.)

### 🍎 macOS  *(⚠️ instructions below need testing on a real Mac)*

1. **Install Docker.** Download **Docker Desktop** from
   <https://www.docker.com/products/docker-desktop/> and install it like any
   Mac app. Open it once and leave it running (you'll see a whale icon in the
   menu bar). *(Alternatively, [OrbStack](https://orbstack.dev) is a lighter,
   faster replacement for Docker Desktop on Mac — either works.)*

2. **Install Homebrew** (a tool that installs other tools). Paste this into
   Terminal and follow its prompts:

   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

3. **Install the rest** with Homebrew:

   ```bash
   brew install k3d kubectl helm make
   ```

   (`make` and `git` usually come with Apple's developer tools; if `make`
   isn't found later, run `xcode-select --install`.)

### 🪟 Windows  *(⚠️ instructions below need testing on a real Windows machine)*

On Windows the cleanest path is **WSL** — "Windows Subsystem for Linux" — which
runs a real Ubuntu Linux inside Windows. This demo is built for Linux/Mac
tools, and WSL gives you exactly that without leaving Windows.

1. **Install WSL.** Open **PowerShell as Administrator** (right-click the Start
   button → "Terminal (Admin)" or "Windows PowerShell (Admin)") and run:

   ```powershell
   wsl --install
   ```

   Restart your computer when it asks. After restart, it finishes setting up
   **Ubuntu** and asks you to create a username and password — remember these.

2. **Install Docker Desktop** from
   <https://www.docker.com/products/docker-desktop/>. During install, keep the
   **"Use WSL 2 based engine"** option checked. After installing, open Docker
   Desktop → **Settings → Resources → WSL Integration** and turn it **on** for
   your Ubuntu distribution. Leave Docker Desktop running.

3. **Open the Ubuntu terminal** (Start menu → type `Ubuntu` → Enter). From
   here on, **you are in Linux** — run all the project commands here, not in
   PowerShell. Install the tools:

   ```bash
   sudo apt update && sudo apt install -y make curl
   curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
   sudo snap install kubectl --classic || sudo apt install -y kubectl
   curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
   ```

4. **Raise a system limit** (needed for the automation pipeline; see the box
   in Part 4). In the Ubuntu terminal:

   ```bash
   sudo sysctl fs.inotify.max_user_instances=1024
   echo 'fs.inotify.max_user_instances=1024' | sudo tee -a /etc/sysctl.conf
   ```

### 🐧 Linux

1. **Install Docker** using your distribution's instructions
   (<https://docs.docker.com/engine/install/>), then add yourself to the
   `docker` group so you don't need `sudo` for every command:

   ```bash
   sudo usermod -aG docker $USER    # then log out and back in
   ```

2. **Install the tools:**

   ```bash
   curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
   # kubectl + helm + make: use your package manager, e.g. on Debian/Ubuntu:
   sudo apt update && sudo apt install -y kubectl helm make
   ```

3. **Raise a system limit** (one time — see the box in Part 4):

   ```bash
   sudo sysctl fs.inotify.max_user_instances=1024
   echo 'fs.inotify.max_user_instances=1024' | sudo tee /etc/sysctl.d/99-inotify.conf
   ```

### Check the tools installed

Run each of these. Each should print a version number (not "command not
found"):

```bash
docker version
k3d version
kubectl version --client
helm version
make --version
```

---

## Part 3 — Get the project onto your computer

The simplest way, if you have `git`:

```bash
git clone https://github.com/jp2195/network-automation-demo.git
cd network-automation-demo
```

(No `git`? On the GitHub page click the green **Code** button → **Download
ZIP**, unzip it, then `cd` into the unzipped folder. To `cd` into a folder,
type `cd ` and drag the folder onto the terminal window, then press Enter.)

> **One important note about how this demo updates itself.** This project uses
> "GitOps" — the cluster pulls the network design straight from the project's
> GitHub page, not from the copy on your disk. So **running the official demo
> as-is works out of the box**. But if you want to *change* the design, you
> have to put your changed copy on your own GitHub account (a "fork") and point
> the project at it first. That's an advanced topic — the [README](README.md)
> covers it under "Quickstart". For just trying the demo, ignore this.

---

## Part 4 — Start it

Make sure **Docker is running** (the whale icon / Docker Desktop is open),
then, from inside the project folder:

```bash
make up
```

This one command does everything: creates the tiny Kubernetes cluster, builds
some software images, and installs all the pieces. **It will take 10–20
minutes the first time** and print a lot of text. That's normal.

> ### About that "inotify" warning
> If `make up` prints a warning about `fs.inotify.max_user_instances`, it means
> a Linux system limit is too low and the automation pipeline (the part that
> reacts to network failures) won't fire. The fix is the one-time command shown
> in your OS's install section above (Part 2). The cluster and dashboards still
> work without it — only the automatic incident response is affected. On
> **macOS with Docker Desktop** this limit lives inside Docker's own virtual
> machine and usually doesn't need changing; if the pipeline misbehaves, see
> the troubleshooting runbook.

### How do I know it's ready?

`make up` finishes by printing a status summary. The pieces install
themselves over a few more minutes. Check progress with:

```bash
make status
```

You're looking for the Applications to become **Synced / Healthy**. There are
**21** of them. **20 will go Synced/Healthy on their own**; one called
`dom-synth` stays "OutOfSync" on purpose (it's set to manual) — that's
expected, not a problem.

To watch them settle, you can run this until only `dom-synth` is left:

```bash
kubectl -n argocd get applications
```

---

## Part 5 — Look around (the web pages)

Once things are healthy, open these in your web browser. (If a page doesn't
load immediately, wait a minute — the software may still be starting.)

| What | Address | How to log in |
|---|---|---|
| **Grafana** (dashboards & graphs) | <http://grafana.127-0-0-1.nip.io:8080> | `admin` / `admin` |
| **ArgoCD** (shows all the pieces) | <http://argocd.127-0-0-1.nip.io:8080> | `admin` / run `make status` for the password |
| **NetBox** (the network "source of truth") | <http://netbox.127-0-0-1.nip.io:8080> | `admin` / `admin` |
| **Argo Workflows** (the automation runs) | <http://workflows.127-0-0-1.nip.io:8080> | no login |
| **Clabernetes** (the simulated routers) | <http://clabernetes.127-0-0-1.nip.io:8080> | no login |

(Those `127-0-0-1.nip.io` addresses are a trick that always points back to
your own computer — you don't need to configure anything. `https://` versions
on port `8443` also work but will show a "not secure" warning because the demo
uses a self-signed certificate; the `http://…:8080` versions above avoid that.)

**Start at Grafana.** Open the **Geomap** or **Overview** dashboard to see the
simulated Atlanta-metro fiber network, all green and healthy.

---

## Part 6 — Try the features

This is the fun part: cause a (simulated) network failure and watch the system
detect it, explain it, and respond. All of that is in a dedicated, plain-
language guide:

➡️ **[FEATURES.md](FEATURES.md)** — what every feature does and how to try it.

The quickest taste: cut a fiber link and watch Grafana light up red, then heal
it:

```bash
make demo-cut NODE=hub-i20e INTERFACE=ethernet-1/4
# ...watch Grafana go red, then:
make demo-restore NODE=hub-i20e INTERFACE=ethernet-1/4
```

---

## Part 7 — Stop it

When you're done, tear the whole thing down. This deletes the cluster and
frees the memory and disk it was using. Your project files stay put.

```bash
make down
```

To run it again later, just `make up` again.

---

## Part 8 — When something goes wrong

A few common newcomer snags:

- **"command not found"** — the tool isn't installed or your terminal can't
  find it. Re-check Part 2 for that tool. On Windows, make sure you're in the
  **Ubuntu** terminal, not PowerShell.
- **`make up` errors about Docker** — Docker isn't running. Open Docker Desktop
  (or start the Docker service on Linux) and try again.
- **Pages won't load in the browser** — give it a few more minutes; run
  `make status` and wait for apps to be Healthy. Make sure you used the
  `http://…:8080` address exactly.
- **The automatic incident response doesn't fire** (graphs react but no Slack/
  workflow) — almost always the `inotify` limit from Part 2/Part 4. Apply the
  one-time fix and run `make down` then `make up`.
- **Everything is very slow / your fans roar** — this stack is heavy. Close
  other apps; 32 GB RAM helps a lot.

For anything deeper, the maintainers' troubleshooting guide is
[`docs/runbook-troubleshoot.md`](docs/runbook-troubleshoot.md) — it's written
for a more technical reader but the symptom → fix table is useful to anyone.

---

Welcome aboard. When you're ready to actually *use* the demo, head to
**[FEATURES.md](FEATURES.md)**.
