# Independent ChatGPT subscription connection

This implements the official Sign in with ChatGPT (SIWC) open-source-app preview.
It does not reuse Codex or Claude credentials, use backend-api endpoints, or
fall back to API keys/DS. Each installation requires its own explicit setup;
repository code or synthetic checks do not establish an account connection.

## Deliberate setup steps

Use the existing Python environment with PyJWT, cryptography and httpx. From
this checkout, set `PYTHONPATH=src` and invoke `python -B -m chatgpt_login`.
Every invocation requires `--auth-dir /private/app/auth` and
`--vault /actual/ob/vault`; paths here are placeholders, not installed paths.
The auth directory must be outside the vault and Git, owner-private, and must
not be a Codex/Claude/AWS directory. The fixed file `ob-chatgpt.sqlite3` must
be owner-only, owned by this user, and have only one hard link. Do not include
it in a vault backup. It is a sensitive local credential store, not encrypted
at rest by this application; OS disk/account protection still applies.

1. `login`: opens only a local page. Click **Continue with ChatGPT**, then
   select the account/workspace and consent on OpenAI's page. No inference is
   performed. The callback listener binds only 127.0.0.1 and closes after five
   minutes, including slow/incomplete connections. Declined plan permission
   retains verified identity but cannot make inference requests.
2. `status`: lists saved profiles and their model/paused/plan-permission status.
   `--profile main` is the default label; use a different label to add another
   account. Existing labels must retain their verified subject and client ID.
3. `models`: fetches the selected account's current visible model catalog.
   `select-model MODEL_SLUG` selects only an actually listed model. No default
   model is silently substituted. Stop/recreate the adapter after changing it.
4. `smoke`: makes one explicitly requested inference using a short, hard-coded
   fictional meal sentence. It returns success/model/provider usage, not source
   text. This consumes the selected account's plan usage if authorized.
5. Only after this succeeds, wire the real app's configuration below. A real-day
   quality/privacy/usage trial still requires its separately agreed data scope.
6. `resume` explicitly clears a paused state after review. `logout` attempts
   remote revocation and clears local tokens while retaining the registration;
   it reports if remote revocation could not be confirmed.

The CLI does not read `.env`, Codex auth, Claude config or a real transcript.
Its `--vault` argument establishes the forbidden credential placement boundary;
it does not open that vault. Login/status/model selection do not enable night
processing, note retirement or takeover. Keep **Manage usage** available at
https://chatgpt.com/#settings/Usage .

## App configuration after authorization and smoke

```yaml
night_clerk:
  enabled: true
  automatic: false
  provider: chatgpt_subscription
  auth_dir: /private/app/auth
  profile: main
  input_profile: scene_word_mask
  lexicon_path: /private/app/clerk-words.json
  approved_input_revision: "<reviewed profile hash>"
  automatic_start_day: "<explicit first YYYY-MM-DD>"
  input_budget: 16000
  max_calls_per_run: 100
  max_calls_per_day: 64
  timeout_seconds: 45
```

These are setup examples, not this machine's live configuration. Defaults remain
`enabled=false`, `automatic=false`, and no provider/auth path. An explicit
provider enables the factory through DailyContinuityService; an injected fake
adapter still takes precedence in tests. Missing/invalid credentials fail
closed, with no alternate billing path. For a single-user self-hosted runtime,
follow the official [self-hosted VM procedure](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms):
complete OAuth locally, securely transfer only the selected registration,
preserve a distinct persistent VM host ID, and let only the VM own subsequent
refreshes. Do not clone the laptop host ID or leave two workers racing rotating
refresh tokens. Keep runtime credentials on a private persistent mount outside
both the vault and source control. This is not a shared/public token proxy.

## Boundary and recovery

Only existing validated clerk payloads can reach the adapter. It has no shell,
file access or other model tools. Original native thinking stays viewable in
the archive but never becomes clerk input. Full recovery backups preserve the
archive, not SIWC credentials. No transcript or auth material is logged by the
new login/adapter code.

OAuth uses a stable host ID, separate profile registrations, PKCE, state and
nonce; verifies signed ID-token issuer/audience/expiry/subject and authorized
party for multiple audiences. Only granted scopes permit inference. Registered
client IDs survive failed code exchange. SQLite transactions serialize token
refreshes and atomically replace rotating tokens, scope and expiry. Invalidated
sessions clear tokens and require login; temporary failures preserve credentials
and pause, without automatic retry loops. Protected records must remain private
and outside source control. Only documented fixed HTTPS endpoints receive tokens;
redirects and ambient HTTP environment settings are disabled.

Inference uses `store=false`, `stream=true`, developer instructions and an input
array. Unsupported preview fields are omitted. Final assistant text is assembled
from validated `response.output_item.done` messages when the terminal snapshot
omits its output. Declared items, indices, IDs and response identity must agree.
Parsing waits for `response.completed`; partial deltas, reasoning, tools, EOF and
failures cannot publish facts. Provider usage is taken
from that terminal response, never model-generated JSON. Midstream or HTTP
usage-limit errors persist a pause. App-ineligible errors retain their precise
state rather than suggesting repeated OAuth. Whole HTTP JSON size participates
in chunking, selection grouping and the durable request upper-bound ledger.
Unknown consumption after interruption remains unknown.

## Verification limits

Generated signing keys, fake token endpoints and synthetic SSE test protocol
behavior without a real login. A separate short local socket test exercises
absolute timeout and cleanup without a browser or external request. Actual
account eligibility, consent, model access, refresh behavior, network path,
completed inference and real usage are still unverified. They require human
browser authorization and the explicit smoke step. Do not describe this
candidate as connected or already running every night.

Official sources checked on 2026-11-03:

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [Identity verification](https://developers.openai.com/siwc/website)
