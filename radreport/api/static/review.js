/* Review-screen instrumentation (§7.2, §6.7).
 *
 * Two jobs the server cannot do:
 *
 * 1. `active_edit_seconds` — FOCUS time, not wall clock. §15.2's commercial
 *    argument rests entirely on it and plan §0.2 puts the break-even bar at
 *    18-36 seconds per report, so counting the coffee break would flatter the
 *    number in the direction that matters. The timer stops on blur and on
 *    IDLE_MS of no interaction, and the server clamps whatever we report to
 *    the wall clock anyway.
 *
 * 2. Per-field click-to-listen, off `provenance_span.audio_start_ms`. This is
 *    what makes a provenance span useful to a human rather than an audit
 *    artefact.
 */

const IDLE_MS = 20000;

export class FocusTimer {
  constructor() {
    this.activeMs = 0;
    this.openedAt = Date.now();
    this.runningSince = document.hasFocus() ? Date.now() : null;
    this.lastInput = Date.now();

    window.addEventListener("focus", () => this.resume());
    window.addEventListener("blur", () => this.pause());
    document.addEventListener("visibilitychange", () =>
      document.hidden ? this.pause() : this.resume(),
    );
    for (const evt of ["keydown", "pointerdown", "input", "scroll"]) {
      document.addEventListener(evt, () => this.poke(), { passive: true });
    }
    setInterval(() => this.checkIdle(), 1000);
  }

  poke() {
    this.lastInput = Date.now();
    this.resume();
  }

  resume() {
    if (this.runningSince === null) this.runningSince = Date.now();
  }

  pause() {
    if (this.runningSince !== null) {
      this.activeMs += Date.now() - this.runningSince;
      this.runningSince = null;
    }
  }

  checkIdle() {
    // Idle counts as away. A screen left open on a draft is not review time.
    if (this.runningSince !== null && Date.now() - this.lastInput > IDLE_MS) {
      this.pause();
    }
    const el = document.getElementById("active-seconds");
    if (el) el.textContent = String(this.activeSeconds());
  }

  activeSeconds() {
    const running = this.runningSince === null ? 0 : Date.now() - this.runningSince;
    return Math.round((this.activeMs + running) / 1000);
  }

  wallClockSeconds() {
    return Math.round((Date.now() - this.openedAt) / 1000);
  }
}

export function wireClickToListen(audio) {
  document.querySelectorAll("[data-audio-start]").forEach((el) => {
    el.addEventListener("click", () => {
      const start = Number(el.dataset.audioStart) / 1000;
      const end = Number(el.dataset.audioEnd) / 1000;
      audio.currentTime = start;
      audio.play();
      // Stop at the end of the cited span: the point is to hear *this* field's
      // evidence, not the rest of the dictation after it.
      const stop = () => {
        if (audio.currentTime >= end) {
          audio.pause();
          audio.removeEventListener("timeupdate", stop);
        }
      };
      audio.addEventListener("timeupdate", stop);
    });
  });
}

export function collectEdits(root) {
  const edits = [];
  root.querySelectorAll("[data-field-value-id]").forEach((el) => {
    const input = el.querySelector(".field-input");
    if (!input) return;
    if (input.value !== input.dataset.original) {
      edits.push({ field_value_id: el.dataset.fieldValueId, value_text: input.value });
    }
  });
  return edits;
}
