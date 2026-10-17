---
title: "Your first week"
description: "Roll it out safely: observe, then suggest, then let it act on the containers where that is safe."
---

1. **Week 1 — `observe`.** Read the incidents. You are checking whether the
   diagnoses are right, and whether the thresholds match how your host actually
   behaves. Tune `DOCTOR_CPU_THRESHOLD`, `DOCTOR_LOG_ERROR_BURST` and
   `DOCTOR_POLL_INTERVAL` here.
2. **Week 2 — `suggest`.** It now proposes actions. Approve them by hand from
   the dashboard and see whether the proposals were the right call.
3. **Then — `auto`, selectively.** Rather than flipping the whole host, leave
   the global mode at `suggest` and opt individual containers in with
   `doctor.mode=auto` on the ones where an unattended restart is genuinely safe.
