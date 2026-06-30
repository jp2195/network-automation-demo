# Working with secrets in this repo

This is a public demo. **No credential committed in this repository is
real, and no manifest in this repository commits a placeholder Secret you'd
need to replace.** Anything that *looks* credential-shaped is either:

- a documented default (e.g. NetBox / Grafana `admin / admin`), OR
- a literal demo string with no authority anywhere (e.g. the NetBox
  `secret_key` is `atlas-demo-not-secret-but-50-plus-chars-long-padding-yes`).

The NetBox **API token** isn't committed at all: the netbox-seed Job mints one
at runtime (`tokens/provision/`) and patches it into the `argo-events/netbox-api`
Secret, which the eventing/maintenance WorkflowTemplates read via `secretKeyRef`.

For the one credential where a real value matters in a real demo —
**Slack** — we don't ship a Secret manifest at all. The Argo Workflow
references the `slack-bot` Secret with `optional: true`, and `notify.py`
short-circuits to stderr-printed Block Kit payloads if the env vars
aren't populated. So:

- **Default state** (no extra setup): demo runs, alert flow fires, Block
  Kit messages appear in the workflow step pod's `kubectl logs`.
- **You want real Slack**: create the `slack-bot` Secret in-cluster
  yourself. One of the patterns below.

The patterns generalize to any other secret you might want to add later
(production NetBox tokens, real provider credentials, etc.). The file
patterns `*.secret.yaml` and the directory `secrets.local/` are
gitignored so a careless `git add -A` won't catch them.

## AI incident analyst (`ai-analyst` Secret)

The AI lane follows the same optional-Secret pattern as Slack. With no
Secret, every `ai-analyze-*` workflow step prints `AI disabled` and
exits 0 — the deterministic pipeline never depends on it.

To enable it, point the Secret at any OpenAI-compatible endpoint.
A hosted frontier model gives the best analyses with zero tuning:

```
kubectl create secret generic ai-analyst \
  --namespace argo-events \
  --from-literal=base_url='https://api.openai.com/v1' \
  --from-literal=api_key='sk-YOUR-REAL-KEY' \
  --from-literal=model='gpt-5.2'
```

"OpenAI-compatible" covers more than OpenAI — Anthropic
(`base_url=https://api.anthropic.com/v1/`, a `claude-*` model) and
Gemini (`base_url=https://generativelanguage.googleapis.com/v1beta/openai/`)
both expose compatible endpoints, so the same three keys work.

### Zero-cost local option (Ollama, live-verified recipe)

Small local models work, but need three things or they fail in
well-understood ways (see the troubleshooting runbook): a context
window big enough for tool results, thinking disabled, and a low
temperature. One-time Ollama server config (the same override you
already need so k3d pods can reach the host at all):

```
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
Environment="OLLAMA_CONTEXT_LENGTH=16384"
```

(No sudo? `ollama create qwen3.5-16k -f-` with `FROM qwen3.5:9b` +
`PARAMETER num_ctx 16384` achieves the same per-model.)

On macOS / Apple Silicon (the host this repo is tested on), set the same env on
the Ollama server — `launchctl setenv OLLAMA_HOST 0.0.0.0`, or just run
`OLLAMA_HOST=0.0.0.0 OLLAMA_CONTEXT_LENGTH=16384 ollama serve`. Prefer an **MLX**
build there: it runs on Metal / unified memory and is faster and lighter than
the default GGUF build for the same weights.

Then the Secret. Verified end-to-end on Apple Silicon with `qwen3.6:35b-mlx`
(and earlier with the smaller `qwen3.5:9b`) — both correctly told an admin
disable from a real fiber cut at 0.95 confidence, once the gNMI tool was pointed
at SR Linux native paths (not OpenConfig `/state/...`):

```
kubectl create secret generic ai-analyst \
  --namespace argo-events \
  --from-literal=base_url='http://host.k3d.internal:11434/v1' \
  --from-literal=api_key='ollama' \
  --from-literal=model='qwen3.6:35b-mlx' \   # or qwen3.5:9b for a smaller/faster lane
  --from-literal=reasoning_effort='none' \
  --from-literal=temperature='0.2'
```

A bigger local model can take several minutes per incident (multiple tool-call
rounds); the analyst Workflow allows for it (`activeDeadlineSeconds: 900`).

### Where to run the model — host, Docker, or cloud

The analyst only needs a URL; it does not care where the model runs. The
cluster itself stays GPU-free — **run the model outside the cluster** and point
the Secret at it. (Giving the k3d cluster direct GPU access is heavy plumbing —
NVIDIA container toolkit + a CUDA-enabled k3s node image — and deliberately out
of scope here.)

| Where | When to choose it | `base_url` to use |
|---|---|---|
| **Cloud endpoint** (OpenAI / Anthropic / Gemini) | Best analysis quality, zero local setup, you don't mind per-call cost | the provider URL above |
| **Ollama on the host** *(recommended local default — the verified recipe above)* | Free, simplest, uses your host GPU if Ollama already does | `http://host.k3d.internal:11434/v1` |
| **Ollama (or vLLM) in a Docker container** | You'd rather not install a model server on the host, want GPU isolation, or want to pin a specific image | publish a host port and use `http://host.k3d.internal:<port>/v1` |

**Docker sidecar (Ollama with GPU), no host install:**

```
docker run -d --gpus=all -p 11434:11434 -v ollama:/root/.ollama \
  -e OLLAMA_HOST=0.0.0.0 -e OLLAMA_CONTEXT_LENGTH=16384 \
  --name ollama ollama/ollama
docker exec ollama ollama pull qwen3.5:9b
```

Then use the **same Secret as the host recipe** (`base_url=http://host.k3d.internal:11434/v1`).
Publishing the port (`-p 11434:11434`) is what makes `host.k3d.internal`
reach it; that's more reliable than trying to resolve a container name from
inside a pod.

**vLLM (GPU, OpenAI-compatible):** vLLM serves an OpenAI-style `/v1` API, so it
works exactly like the above. It's higher-throughput but VRAM-hungry and is
overkill for this lane's one structured call per incident — reach for it only
if you already run it.

```
docker run -d --gpus=all -p 8000:8000 \
  vllm/vllm-openai --model Qwen/Qwen2.5-7B-Instruct --api-key local
```

Secret: `base_url=http://host.k3d.internal:8000/v1`, `api_key=local`,
`model=Qwen/Qwen2.5-7B-Instruct`. vLLM has a large native context, so you can
drop `reasoning_effort`/`temperature` unless the chosen model is a thinker.

> Quality note from testing: among local models, `qwen3.6:35b` gave the
> sharpest analyses, `qwen3.5:9b` was a solid lighter choice, and the Gemma
> family worked but shallower. Bigger isn't always better — tool-use diligence
> matters more than raw size. A cloud frontier model still beats all of them.

### Optional tuning keys

All optional; omitted keys are simply not sent to the endpoint:

- `reasoning_effort` — `none` disables thinking on local models that
  otherwise burn the whole output budget on reasoning. Cloud reasoning
  models accept their documented values (`minimal`, `low`, ...).
- `temperature` — `0.2` steadies small local models. Omit for cloud
  reasoning models (they reject non-default temperature).
- `max_requests` — backstop on model requests per incident
  (default 24). Raise for deeper dives with a strong model.

The analyst is read-only by construction (gNMI Get-only module, input
allowlists on every tool) and advisory forever — it never executes
remediation. Remove with `kubectl -n argo-events delete secret ai-analyst`.

## What lives in the repo today (and isn't a secret)

| Value | File | Why it's safe to commit |
|---|---|---|
| NetBox API token (`argo-events/netbox-api` Secret) | not committed — minted at runtime by the netbox-seed Job and read via `secretKeyRef` in the eventing/maintenance WorkflowTemplates | Provisioned in-cluster, never in git. Consumers mark the ref `optional: true` so they start before the seed completes. |
| NetBox superuser `admin` / `admin` | `workloads/netbox/chart-values.yaml` | Demo default; NetBox runs behind in-cluster ingress on a `nip.io` host bound to localhost. |
| NetBox `secret_key` `"atlas-demo-not-secret-…"` | `workloads/netbox/chart-values.yaml` | Self-labelling demo string used only for Django session signing on a single-laptop cluster. |
| Grafana admin `admin` / `admin` | `platform/values/kube-prometheus-stack.yaml` | Demo default. |
| SR Linux gNMI password `NokiaSrl1!` | `workloads/gnmic/targets.yaml`, `workloads/eventing/wft-cut-fiber.yaml` | The publicly documented default for SR Linux containers. |

None of these should be used in any environment that anyone other than
you can reach.

## What you should NOT put in this repo

- Real Slack bot tokens (`xoxb-…` with actual values)
- Real cloud provider credentials of any kind
- Real NetBox API tokens for any deployment that has real data
- Production database passwords, certificates, private keys
- Anything from your password manager

If you need any of those things to run this demo against your own
infrastructure, use one of the override patterns below.

## Override pattern A: hand-applied in-cluster Secret (quick-start)

Simplest. Good for a personal laptop where you want real Slack
notifications. The Secret is created in the cluster, never goes near git.

After `make up`, before or after the eventing Application has synced:

```
kubectl create secret generic slack-bot \
  --namespace argo-events \
  --from-literal=bot_token='xoxb-YOUR-REAL-TOKEN' \
  --from-literal=channel_id='C0YOUR-REAL-CHANNEL'
```

Run a workflow (e.g. `make demo-cut`) — `notify.py` will pick the values
up via the workflow step's optional `secretKeyRef` and post to Slack
for real.

If you'd rather keep your real values in a YAML file you can re-apply
after a cluster rebuild, drop them in `secrets.local/slack-bot.yaml`
(the directory is gitignored):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: slack-bot
  namespace: argo-events
type: Opaque
stringData:
  bot_token: "xoxb-YOUR-REAL-TOKEN"
  channel_id: "C0YOUR-REAL-CHANNEL"
```

Then `kubectl apply -f secrets.local/slack-bot.yaml`. ArgoCD won't fight
you over this Secret because it's not declared in any tracked manifest —
ArgoCD only manages resources it owns.

**Trade-off**: every fresh cluster (`make down && make up`) needs this
one command again.

## Override pattern B: sealed-secrets (git-stored, encrypted)

[`bitnami-labs/sealed-secrets`](https://github.com/bitnami-labs/sealed-secrets)
lets you commit *encrypted* Secret manifests to git. Only the in-cluster
controller can decrypt them. Safe in a public repo.

This isn't deployed by default. To add it:

1. Add an entry to `argocd/manifests/platform/sealed-secrets.yaml`:

   ```yaml
   # argocd/manifests/platform/sealed-secrets.yaml
   name: sealed-secrets
   namespace: kube-system
   syncWave: "-1"
   chart:
     repo: https://bitnami-labs.github.io/sealed-secrets
     name: sealed-secrets
     version: 2.16.2
     releaseName: sealed-secrets
     values: platform/values/sealed-secrets.yaml
   ```

2. After the controller is running, fetch its public cert:

   ```
   kubeseal --fetch-cert > pub-cert.pem
   ```

3. Build the cleartext Secret locally (do **not** commit), then seal:

   ```
   cat <<EOF > /tmp/slack-bot.yaml
   apiVersion: v1
   kind: Secret
   metadata:
     name: slack-bot
     namespace: argo-events
   type: Opaque
   stringData:
     bot_token: "xoxb-YOUR-REAL-TOKEN"
     channel_id: "C0YOUR-REAL-CHANNEL"
   EOF

   kubeseal --cert pub-cert.pem -o yaml < /tmp/slack-bot.yaml \
     > workloads/eventing/slack-bot-sealed.yaml
   rm /tmp/slack-bot.yaml
   ```

4. Add `slack-bot-sealed.yaml` to
   `workloads/eventing/kustomization.yaml`'s `resources`. Commit. Push.
   The encrypted blob is safe in git; only your cluster's sealed-secrets
   controller can decrypt it.

5. **Back up `kube-system/sealed-secrets-key*` Secrets** — those are the
   controller's private keys. Without them, your sealed secrets become
   unrecoverable on a cluster rebuild.

## Override pattern C: SOPS + age (alternative to sealed-secrets)

[`mozilla/sops`](https://github.com/mozilla/sops) with `age` keys is a
popular alternative. Encrypted files live in git; ArgoCD decrypts via
[argocd-vault-plugin](https://github.com/argoproj-labs/argocd-vault-plugin)
or a SOPS-aware Kustomize plugin. Heavier setup; not covered in this
demo.

## Audit checklist before pushing

Run this before any push, especially the first push to a public repo:

```
# Real-looking credential prefixes (length-bounded so we don't false-match
# things like SR Linux's `mask-length-range` keyword).
git grep -E "xoxb-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}"

# Files that should never be tracked
git ls-files | grep -E "^secrets\.local/|\.secret\.yaml$"
```

Both should return nothing. If either has output, do not push.

## What's safe to make public

The repository as committed is safe to make public — there is no
placeholder Secret manifest, the only "credentials" exposed are
documented defaults that wouldn't authenticate against anything real,
and a clean `git grep` of credential prefixes returns nothing.

The instant a real value lands in a tracked file, **either move it to
one of the override patterns above or change the repo's visibility back
to private**.
