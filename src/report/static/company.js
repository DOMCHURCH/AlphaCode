/* The question box, and only the question box.

   Loaded solely on pages where the model resolved at startup. Everything else
   on this page is already rendered by the server, so if this file never
   arrives the reader has lost a convenience, not the drawing.

   The answer is written with textContent, never innerHTML: it is model output,
   which is untrusted text no matter how well-behaved the model usually is. */
"use strict";

(function () {
  const box = document.getElementById("askbox");
  if (!box) return;
  const form = document.getElementById("qform");
  const input = document.getElementById("qinput");
  const send = document.getElementById("qsend");
  const out = document.getElementById("qanswer");
  const ticker = box.dataset.ticker;
  let busy = false;

  function show(text, kind) {
    out.hidden = false;
    out.className = "qanswer" + (kind ? " " + kind : "");
    out.textContent = text;
  }

  async function ask(question) {
    if (busy || !question.trim()) return;
    busy = true;
    send.disabled = true;
    // Say what is happening. A silent three-second pause reads as a dead button.
    show("Reading the numbers…", "pending");
    try {
      const r = await fetch("/company/" + encodeURIComponent(ticker) + "/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        // The server's detail is written for a reader; pass it through rather
        // than replacing it with a generic failure.
        show(body.detail || "That did not work. Try again in a moment.", "bad");
      } else {
        show(body.answer || "No answer came back.", "");
      }
    } catch (e) {
      show("Could not reach the server. Check your connection and try again.", "bad");
    } finally {
      busy = false;
      send.disabled = false;
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    ask(input.value);
  });

  for (const chip of box.querySelectorAll(".qchip")) {
    chip.addEventListener("click", function () {
      // Put it in the box as well as sending it, so the reader can see what
      // was asked and edit it into their own question.
      input.value = chip.textContent;
      ask(chip.textContent);
    });
  }
})();
