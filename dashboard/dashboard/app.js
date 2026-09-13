const STREAMS = {
  main: "/head/stream",
  left: "/left/stream",
  right: "/right/stream"
};

const RANGE = {
  min: 0.20,
  max: 0.35
};

const cameraEls = {
  main: document.getElementById("mainCamera"),
  left: document.getElementById("leftCamera"),
  right: document.getElementById("rightCamera")
};

const cameraState = {
  main: null,
  left: null,
  right: null
};

const anomalyEls = {
  left: document.getElementById("leftAnomaly"),
  right: document.getElementById("rightAnomaly")
};

let rawViewsStarted = false;
let lastRangeState = null;
let lastExpression = null;

const events = [];

const placeholder =
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent(`
    <svg xmlns="http://www.w3.org/2000/svg" width="800" height="500">
      <rect width="800" height="500" fill="#0d1922"/>
      <text x="400" y="250" text-anchor="middle"
        font-family="Inter,Arial" font-size="24" fill="#9ba8b4">
        NO FEED
      </text>
    </svg>
  `);

function startRawViews() {
  if (rawViewsStarted) {
    return;
  }

  rawViewsStarted = true;

  ["rawHeadCamera", "lensLeftCamera", "lensRightCamera"].forEach((id) => {
    const image = document.getElementById(id);

    function connect() {
      image.onerror = () => {
        image.onerror = null;
        image.src = placeholder;
        setTimeout(connect, 2500);
      };

      image.src = `/raw/head/stream?view=${id}&t=${Date.now()}`;
    }

    connect();
  });
}

function timeLabel() {
  return new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  });
}

function addEvent(title, detail, kind = "INFO") {
  events.unshift({
    time: timeLabel(),
    title,
    detail,
    kind
  });

  if (events.length > 60) {
    events.pop();
  }

  renderEvents();
}

function renderEvents() {
  const element = document.getElementById("eventLog");

  if (!events.length) {
    element.innerHTML =
      '<div class="event-empty">Camera and verification events will appear here.</div>';
    return;
  }

  element.innerHTML = events
    .map(
      (event) => `
        <div class="event-row">
          <time class="event-time">${event.time}</time>
          <div class="event-copy">
            <strong>${event.title}</strong>
            <small>${event.detail}</small>
          </div>
          <span class="event-kind ${
            event.kind === "PASS"
              ? "event-pass"
              : event.kind === "FAIL"
                ? "event-fail"
                : ""
          }">${event.kind}</span>
        </div>
      `
    )
    .join("");
}

function setCameraState(key, ok) {
  const badge = document.getElementById(`${key}Badge`);

  if (!badge) {
    return;
  }

  badge.innerHTML = `<i></i> ${ok ? "Live" : "Offline"}`;
  badge.className = `status-badge ${ok ? "live" : "offline"}`;

  const capitalizedKey = key[0].toUpperCase() + key.slice(1);
  const channelDot = document.getElementById(`channel${capitalizedKey}`);
  const channelText = document.getElementById(
    `channel${capitalizedKey}Text`
  );

  if (channelDot) {
    channelDot.className = ok ? "live" : "offline";
  }

  if (channelText) {
    channelText.textContent = ok ? "Live" : "Offline";
  }

  if (cameraState[key] !== ok) {
    if (cameraState[key] !== null) {
      addEvent(
        `${capitalizedKey} camera ${ok ? "connected" : "offline"}`,
        ok ? "Video stream restored" : "No frames received",
        ok ? "INFO" : "ALERT"
      );
    }

    cameraState[key] = ok;
    updateSystemStatus();
  }
}

function connectCamera(key) {
  const image = cameraEls[key];

  if (!image) {
    return;
  }

  image.onload = () => {
    if (!image.src.startsWith("data:image")) {
      setCameraState(key, true);
    }
  };

  image.onerror = () => {
    setCameraState(key, false);
    image.onerror = null;
    image.onload = null;
    image.src = placeholder;

    setTimeout(() => connectCamera(key), 1500);
  };

  image.src = `${STREAMS[key]}?t=${Date.now()}`;
}

function updateSystemStatus() {
  const live = Object.values(cameraState).filter(Boolean).length;

  document.getElementById("systemStatus").textContent =
    live === 3
      ? "All systems operational"
      : `${live}/3 cameras operational`;
}

function setDecision(pass, source) {
  if (source) {
    addEvent(
      `Subject ${pass ? "passed" : "failed"}`,
      source,
      pass ? "PASS" : "FAIL"
    );
  }
}

async function sendButtonPress(endpoint, label) {
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        value: 1
      }),
      cache: "no-store"
    });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    await response.json();

    addEvent(`${label} button sent`, "API value 1", "INFO");
  } catch (error) {
    addEvent(
      `${label} button failed`,
      error instanceof Error ? error.message : "Unable to reach local API",
      "ALERT"
    );
  }
}

function showDistance(data) {
  const banner = document.getElementById("distanceBanner");
  const value = document.getElementById("poseDistance");

  banner.classList.remove("in-range", "out-range");

  if (!data?.valid || !Number.isFinite(data.distance_m)) {
    value.textContent = "--";
    document.getElementById("dataDistance").textContent = "--";
    document.getElementById("dataBearing").textContent = "--";
    document.getElementById("dataPeople").textContent = data?.people ?? 0;
    lastRangeState = null;
    return;
  }

  const distance = data.distance_m;
  const inRange = distance >= RANGE.min && distance <= RANGE.max;

  value.textContent = `${distance.toFixed(2)}m`;
  banner.classList.add(inRange ? "in-range" : "out-range");

  if (lastRangeState !== inRange) {
    addEvent(
      inRange
        ? "Subject entered distance range"
        : "Subject outside distance range",
      `${distance.toFixed(2)} m · range ${RANGE.min.toFixed(
        2
      )}–${RANGE.max.toFixed(2)} m`,
      "ALERT"
    );

    lastRangeState = inRange;
  }

  document.getElementById("dataDistance").textContent =
    `${distance.toFixed(2)} m`;

  document.getElementById("dataBearing").textContent = Number.isFinite(
    data.bearing_rad
  )
    ? `${((data.bearing_rad * 180) / Math.PI).toFixed(1)}°`
    : "--";

  document.getElementById("dataPeople").textContent = data.people ?? 0;
}

async function pollDistance() {
  try {
    const response = await fetch("/pose/distance", {
      cache: "no-store"
    });

    if (!response.ok) {
      throw new Error("Distance request failed");
    }

    showDistance(await response.json());
  } catch {
    showDistance(null);
  }

  setTimeout(pollDistance, 200);
}

function showExpression(data) {
  const banner = document.getElementById("expressionBanner");
  const value = document.getElementById("poseExpression");
  const confidence = document.getElementById("expressionConfidence");

  banner.classList.toggle(
    "detected",
    Boolean(data?.valid && data.primary_emotion)
  );

  if (!data?.valid || !data.primary_emotion) {
    value.textContent = "--";
    confidence.textContent = "No face";
    return;
  }

  value.textContent = data.primary_emotion;

  confidence.textContent = Number.isFinite(data.confidence)
    ? `${Math.round(data.confidence * 100)}%`
    : "";

  if (lastExpression !== data.primary_emotion) {
    addEvent(
      "Expression detected",
      `${data.primary_emotion}${
        confidence.textContent ? ` · ${confidence.textContent}` : ""
      }`,
      "VISION"
    );

    lastExpression = data.primary_emotion;
  }

  document.getElementById("dataExpressionLatency").textContent =
    Number.isFinite(data.latency_ms)
      ? `${Math.round(data.latency_ms)} ms`
      : "--";

  const scores = data.scores || {};

  document.getElementById("expressionScoreGrid").innerHTML = Object.entries(
    scores
  )
    .map(
      ([name, score]) => `
        <div>
          <span>${name}</span>
          <strong>${Math.round(Number(score) * 100)}%</strong>
          <b style="--score:${Math.max(
            0,
            Math.min(100, Number(score) * 100)
          )}%"></b>
        </div>
      `
    )
    .join("");
}

async function pollExpression() {
  try {
    const response = await fetch("/expression", {
      cache: "no-store"
    });

    if (!response.ok) {
      throw new Error("Expression request failed");
    }

    showExpression(await response.json());
  } catch {
    showExpression(null);
  }

  setTimeout(pollExpression, 500);
}

function metric(value, unit = "m") {
  return Number.isFinite(value) ? `${value.toFixed(3)} ${unit}` : "--";
}

function showArm(prefix, arm) {
  document.getElementById(`${prefix}X`).textContent = metric(arm?.x_m);
  document.getElementById(`${prefix}Y`).textContent = metric(arm?.y_m);
  document.getElementById(`${prefix}Z`).textContent = metric(arm?.z_m);

  document.getElementById(`${prefix}Age`).textContent =
    Number.isFinite(arm?.age_ms) ? `${Math.round(arm.age_ms)} ms` : "--";
}

async function pollTelemetry() {
  try {
    const response = await fetch("/data", {
      cache: "no-store"
    });

    if (!response.ok) {
      throw new Error("Telemetry request failed");
    }

    const data = await response.json();

    if (data.buttons?.left_anomaly === 1) {
      setAnomaly("left", true);
    }

    if (data.buttons?.right_anomaly === 1) {
      setAnomaly("right", true);
    }

    showArm("left", data.left);
    showArm("right", data.right);

    document.getElementById("dataLedMode").textContent =
      data.led?.mode?.replace("_", " ") || "--";

    document.getElementById("telemetryUpdated").textContent = data.ready
      ? "Live BBOS data"
      : data.message;
  } catch {
    document.getElementById("telemetryUpdated").textContent = "Unavailable";
  }

  setTimeout(pollTelemetry, 500);
}

function setAnomaly(side, show) {
  anomalyEls[side].hidden = !show;
}

function clearAnomalies() {
  document.getElementById("leftAnomaly").hidden = true;
  document.getElementById("rightAnomaly").hidden = true;
}

async function startBackend() {
  try {
    const response = await fetch("/api/start", {
      method: "POST",
      cache: "no-store"
    });

    if (!response.ok) {
      throw new Error(`Start API returned ${response.status}`);
    }

    addEvent("Backend started", "API /api/start", "INFO");
  } catch (error) {
    addEvent(
      "Backend start failed",
      error instanceof Error ? error.message : "Unable to reach backend",
      "ALERT"
    );
  }
}


document.getElementById("nextButton").addEventListener("click", () => {
  clearAnomalies();
  startBackend();
});

document.getElementById("clearLog").addEventListener("click", () => {
  events.length = 0;
  renderEvents();
});

document.getElementById("passButton").addEventListener("click", () => {
  setDecision(true, "Manual operator decision");
  sendButtonPress("/api/pass", "Pass");
});

document.getElementById("failButton").addEventListener("click", () => {
  setDecision(false, "Manual operator decision");
  sendButtonPress("/api/fail", "Fail");
});

document
  .getElementById("leftAnomalyButton")
  .addEventListener("click", () => {
    setAnomaly("left", true);
    sendButtonPress("/api/left-anomaly", "Left anomaly");
  });

document
  .getElementById("rightAnomalyButton")
  .addEventListener("click", () => {
    setAnomaly("right", true);
    sendButtonPress("/api/right-anomaly", "Right anomaly");
  });

function routeView() {
  const requested = location.hash.slice(1);

  const view = ["overview", "cameras", "activity"].includes(requested)
    ? requested
    : "overview";

  document.querySelector(".dashboard").dataset.view = view;

  if (view === "cameras") {
    startRawViews();
  }

  document
    .querySelector(".workspace")
    .setAttribute(
      "aria-label",
      view === "activity"
        ? "Activity event log"
        : view === "cameras"
          ? "All cameras"
          : "Overview"
    );

  document.querySelectorAll("nav a").forEach((link) => {
    const active = link.hash === `#${view}`;

    link.classList.toggle("active", active);

    if (active) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  document.title = "Bouncer";
}

window.addEventListener("hashchange", routeView);

routeView();

document.querySelectorAll(".fullscreen-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const frame = button.closest(".camera-viewport");

    if (document.fullscreenElement) {
      await document.exitFullscreen();
      return;
    }

    if (frame.classList.contains("expanded")) {
      frame.classList.remove("expanded");
      button.textContent = "⛶ Fullscreen";
      return;
    }

    try {
      await frame.requestFullscreen();
    } catch {
      frame.classList.add("expanded");
      button.textContent = "× Close";
    }
  });
});

document.addEventListener("fullscreenchange", () => {
  document.querySelectorAll(".fullscreen-button").forEach((button) => {
    const active =
      button.closest(".camera-viewport") === document.fullscreenElement;

    button.textContent = active
      ? "× Exit fullscreen"
      : "⛶ Fullscreen";
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll(".expanded").forEach((frame) => {
      frame.classList.remove("expanded");
      frame.querySelector("button").textContent = "⛶ Fullscreen";
    });
  }
});

renderEvents();

addEvent(
  "Dashboard started",
  "Monitoring cameras, distance, and Py-Feat expressions",
  "SYSTEM"
);

Object.keys(STREAMS).forEach(connectCamera);

pollDistance();
pollExpression();
pollTelemetry();