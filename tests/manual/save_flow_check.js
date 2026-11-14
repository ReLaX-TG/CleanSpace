/* CleanSpace — browser regression check for the public save flow.
 *
 * Run on a LOCAL test server only (it creates demo requests with made-up data):
 *   1. Open http://127.0.0.1:5000/ (or a sandbox port), wait until prices are loaded.
 *   2. Paste this whole file into the browser DevTools console and press Enter.
 *   3. It prints PASS/FAIL for every step and returns a summary.
 *
 * Scenario: the server SAVES the request but the browser never receives the answer
 * (the first POST really reaches the server, then the response is thrown away).
 */
(async () => {
  const $ = (id) => document.getElementById(id);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const fire = (el) => el.dispatchEvent(new Event("input", { bubbles: true }));
  const results = [];
  const check = (name, ok, detail = "") => {
    results.push(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
  };

  // Record every POST /api/requests; optionally lose the answer of the next one.
  const realFetch = window.fetch;
  const posts = [];
  let loseNextAnswer = false;
  window.fetch = async (url, options = {}) => {
    const isSave = String(url).includes("/api/requests") && options.method === "POST";
    if (isSave) posts.push(JSON.parse(options.body).requestToken);
    const response = await realFetch(url, options); // the server really processes it
    if (isSave && loseNextAnswer) {
      loseNextAnswer = false;
      await response.text(); // server has answered…
      throw new TypeError("Failed to fetch (simulated lost answer)"); // …but the page never sees it
    }
    return response;
  };

  const status = () => $("save-status").innerText.trim();
  const preview = () => document.querySelector('button[type="submit"]').click();
  const save = async () => { $("save-btn").click(); await wait(1200); };

  try {
    await wait(300);
    // Order: one service line added through the builder.
    $("b-category").value = "rooms"; $("b-category").dispatchEvent(new Event("change", { bubbles: true }));
    $("b-service").value = "deep"; $("b-service").dispatchEvent(new Event("change", { bubbles: true }));
    $("b-quantity").value = "60";
    $("b-add").click();
    // Contact details (made-up).
    const values = {
      name: "Regression " + Math.random().toString(36).slice(2, 7), email: "regression@example.com",
      phone: "+48 600 000 000", city: "Kraków", street: "Testowa 1", apartment: "",
      comment: "made-up regression data",
      date: new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Warsaw" }).format(new Date(Date.now() + 5 * 864e5)),
    };
    for (const [k, v] of Object.entries(values)) { $(k).value = v; fire($(k)); }
    document.querySelector('input[name="time"][value="morning"]').click();

    // 1. Lost answer -> no categorical "Not saved".
    preview();
    loseNextAnswer = true;
    await save();
    check("lost answer shows 'Could not confirm'", status().startsWith("Could not confirm whether the request was saved"), status().split("\n")[0]);
    check("no categorical 'Not saved' after lost answer", !/^Not saved/.test(status()));
    check("retry button offered", $("save-btn").textContent === "Retry with the same request" && !$("save-btn").hidden && !$("save-btn").disabled);
    const t1 = posts[0];

    // 2. Preview again without changes keeps the attempt (no new token, state kept).
    preview();
    check("re-preview keeps the uncertain message", status().startsWith("Could not confirm"));

    // 3. Retry with the same token returns the request saved by the "lost" attempt.
    await save();
    const saved = /Demo request #(\d+) saved/.exec(status());
    check("retry used the same requestToken", posts[1] === t1, `${posts.length} POSTs`);
    check("retry confirmed the saved request", Boolean(saved), status().split("\n")[0]);
    check("server reported it as the earlier save (no duplicate)", /already been saved/.test(status()));
    const firstId = saved ? Number(saved[1]) : null;

    // 4. After confirmed success: re-preview keeps success, Save is not offered again.
    preview();
    check("success kept after re-preview", /Demo request #\d+ saved/.test(status()));
    check("Save hidden after success", $("save-btn").hidden);
    check("'Start a new request' offered", !$("new-request-btn").hidden);
    const postsBefore = posts.length;
    $("save-btn").click(); await wait(300); // hidden button: must do nothing even if clicked
    check("no accidental second save", posts.length === postsBefore);

    // 5. Explicit "Start a new request" -> new token, new request number.
    $("new-request-btn").click();
    await save();
    const second = /Demo request #(\d+) saved/.exec(status());
    check("new request uses a NEW token", posts[posts.length - 1] !== t1);
    check("new request gets a new number", second && Number(second[1]) !== firstId, status().split("\n")[0]);

    // 6. Changing data -> new attempt automatically.
    const lastToken = posts[posts.length - 1];
    $("comment").value = "changed comment"; fire($("comment"));
    preview();
    check("changed data shows Save again", !$("save-btn").hidden && $("save-btn").textContent === "Save demo request");
    await save();
    check("changed data uses a NEW token", posts[posts.length - 1] !== lastToken);
  } finally {
    window.fetch = realFetch;
  }

  const failed = results.filter((r) => r.startsWith("FAIL")).length;
  console.log(results.join("\n"));
  return `${results.length - failed}/${results.length} passed\n` + results.join("\n");
})();
