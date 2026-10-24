# /brag plan: Container Doctor

**What it is:** an AI triage agent for the Docker containers on one Linux host.
Rules decide something is wrong, a model explains why and proposes one fixed
action, and nothing happens until you approve it.

**For whom:** self-hosters running a homelab or a small server full of Compose
stacks, who find out something broke when the app stops working.

**What sets it apart:** it's careful. Plain rules raise the incident (the model
doesn't guess), the model gets five fixed actions to choose from, and you approve
before anything runs. Then it checks that the restart actually helped.

**Hook:** a red heartbeat line and a real `unhealthy` pill: *A container broke at 2 a.m.*
(The demo host's paperless-db really peaks at its memory limit every night at 02:00.)

**Punchline:** *Nothing changes without your OK.*, followed by the approval
going through and the real action log row: *restarted paperless-db;
paperless-db is running and healthy*.

**Tone:** `polished`, with a bit of `default` energy. Dark, calm, confident.

**Visual identity:** the dashboard's own dark theme (`app/web/static/styles.css`):
plane `#0d0d0d`, surface `#1a1a19`, accent `#3987e5`, critical `#d03b3b`, good
`#0ca30c`. Inter / Inter Display, JetBrains Mono for log text. The logo is
the blue tile with the pulse line.

**Source of the footage:** the real dashboard, run against the made-up host in
`demo/` (`python -m demo.serve`). The incident, its diagnosis, the classifier
note, the approval and the action log row all come from that run. Its
markup was saved from the live DOM and is rendered with the dashboard's own
stylesheet and icon sprite. No real names or hosts appear; the demo host
`atlas` is fictional.

## Music

120 BPM (one beat = 0.5 s, one bar = 2 s), A minor → F → C → G. Soft kick,
off-beat hats, a sub bass, a warm pad and a pluck motif. Every cut lands on a
beat. The sound effects use the same key: an airy whoosh on each cut, a soft
click on *Approve*, and an A-major chime when the restart is verified.

## Storyboard (22 s, 44 beats)

| # | Time | Beats | Scene |
|---|------|-------|-------|
| 1 | 0.0–3.0 | 0–6 | **Hook.** On black, a red ECG line draws across the frame. `paperless-db` appears with a real `✕ unhealthy` pill, and the Postgres log line types itself out. The headline is *A container broke at 2 a.m.* |
| 2 | 3.0–7.0 | 6–14 | **Reveal.** The logo tile pops in, the pulse draws, then the wordmark *Container Doctor* and the docs' own tagline: *Know why a container broke, before you open a terminal.* |
| 3 | 7.0–10.5 | 14–21 | **1 · Rules detect.** The real Incidents panel. Rows land one per beat, and the paperless-db row, *awaiting approval*, lights up. Sub: *A crash loop, an error burst, a disk filling up.* |
| 4 | 10.5–15.0 | 21–30 | **2 · A model explains.** The expanded incident: the classifier note, the root cause, and the confidence meter filling to 86%. The "What you should do" steps appear one by one while the camera drifts down. Sub: *It reads the logs, the changes and the stack.* |
| 5 | 15.0–18.5 | 30–37 | **3 · You decide.** The cursor glides to *Approve: restart container* and clicks on beat 32. The pill goes *acting* → *resolved*, and the real action log row slides in as *succeeded*. A small "+45 s" tag marks the doctor's verify delay, which the edit compresses. Sub: *Nothing changes without your OK.* |
| 6 | 18.5–22.0 | 37–44 | **Outro.** Logo and name, *AI triage for self-hosted Docker.*, `docker compose up -d`, and the docs URL. |
