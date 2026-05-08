(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const form = $("#tripForm");
  const storageKey = "trippulse-trip";
  const state = {
    config: { features: { gemini: false, routes: false, maps: false }, googleMapsBrowserKey: "" },
    route: null,
    plan: null,
    lastEnvelope: null,
    map: null,
    directionsService: null,
    directionsRenderer: null,
    mapsLoaded: false,
    toastTimer: null,
    liveTimer: null,
    previewTimer: null,
    autoRouteTimer: null,
  };

  const els = {
    geminiStatus: $("#geminiStatus"),
    routesStatus: $("#routesStatus"),
    mapsStatus: $("#mapsStatus"),
    distanceMetric: $("#distanceMetric"),
    durationMetric: $("#durationMetric"),
    trafficMetric: $("#trafficMetric"),
    sourceMetric: $("#sourceMetric"),
    updatedAt: $("#updatedAt"),
    routeTitle: $("#routeTitle"),
    originMarker: $("#originMarker"),
    destinationMarker: $("#destinationMarker"),
    workspace: $(".workspace"),
    dynamicHeadline: $("#dynamicHeadline"),
    liveClock: $("#liveClock"),
    routeSignalScore: $("#routeSignalScore"),
    routeSignalBar: $("#routeSignalBar"),
    routeSignalText: $("#routeSignalText"),
    paceSignalScore: $("#paceSignalScore"),
    paceSignalBar: $("#paceSignalBar"),
    paceSignalText: $("#paceSignalText"),
    budgetSignalScore: $("#budgetSignalScore"),
    budgetSignalBar: $("#budgetSignalBar"),
    budgetSignalText: $("#budgetSignalText"),
    constraintSignalScore: $("#constraintSignalScore"),
    constraintSignalBar: $("#constraintSignalBar"),
    constraintSignalText: $("#constraintSignalText"),
    itineraryList: $("#itineraryList"),
    adjustmentList: $("#adjustmentList"),
    budgetList: $("#budgetList"),
    riskList: $("#riskList"),
    packingList: $("#packingList"),
    planSource: $("#planSource"),
    planTitle: $("#planTitle"),
    planSummary: $("#planSummary"),
    toast: $("#toast"),
    planButton: $("#planButton"),
    routeButton: $("#routeButton"),
    exportButton: $("#exportButton"),
    resetButton: $("#resetButton"),
  };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    applyDefaultDates();
    hydrateSavedTrip();
    bindEvents();
    renderRoute(clientFallbackRoute(serializeTrip()));
    renderPlanEnvelope({ source: "demo", generatedAt: new Date().toISOString(), plan: clientFallbackPlan(serializeTrip()), warnings: [] });
    startLiveSignals();
    await fetchConfig();
    requestRoute({ silent: true });
  }

  function bindEvents() {
    form.addEventListener("submit", handlePlanSubmit);
    els.routeButton.addEventListener("click", () => requestRoute({ silent: false }));
    els.exportButton.addEventListener("click", exportPlan);
    els.resetButton.addEventListener("click", resetTrip);
    ["origin", "destination", "travelMode"].forEach((id) => {
      $("#" + id).addEventListener("change", () => {
        updateRouteTitle();
        saveTrip();
      });
    });
    form.addEventListener("change", handleTripMutation);
    form.addEventListener("input", handleTripMutation);
    wireAutocomplete("origin", "originPlaceId", "originSuggestions");
    wireAutocomplete("destination", "destinationPlaceId", "destinationSuggestions");
  }

  function handleTripMutation(event) {
    saveTrip();
    updateRouteTitle();
    updateLiveSignals("editing");
    schedulePlanPreview();

    const routeFields = new Set(["origin", "destination", "travelMode", "avoidTolls", "avoidHighways", "avoidFerries"]);
    const target = event.target;
    if (target && routeFields.has(target.id) && textValue("origin").length > 2 && textValue("destination").length > 2) {
      scheduleRouteRefresh();
    }
  }

  async function fetchConfig() {
    try {
      const config = await getJson("/api/config");
      state.config = config;
      setStatus(els.geminiStatus, "Gemini", config.features.gemini);
      setStatus(els.routesStatus, "Routes", config.features.routes);
      setStatus(els.mapsStatus, "Maps", config.features.maps);
      if (config.googleMapsBrowserKey) {
        await loadGoogleMaps(config.googleMapsBrowserKey);
      }
    } catch (error) {
      setStatus(els.geminiStatus, "Gemini", false);
      setStatus(els.routesStatus, "Routes", false);
      setStatus(els.mapsStatus, "Maps", false);
      showToast("Running local demo mode");
    }
  }

  function setStatus(element, label, enabled) {
    element.textContent = enabled ? `${label}: ready` : `${label}: demo`;
    element.classList.remove("ready", "demo", "error");
    element.classList.add(enabled ? "ready" : "demo");
  }

  function scheduleRouteRefresh() {
    window.clearTimeout(state.autoRouteTimer);
    state.autoRouteTimer = window.setTimeout(() => requestRoute({ silent: true }), 1300);
  }

  function schedulePlanPreview() {
    window.clearTimeout(state.previewTimer);
    state.previewTimer = window.setTimeout(() => {
      const payload = serializeTrip();
      renderPlanEnvelope({
        source: "live-preview",
        generatedAt: new Date().toISOString(),
        plan: clientFallbackPlan(payload),
        warnings: [],
      });
    }, 380);
  }

  function startLiveSignals() {
    updateLiveSignals("ready");
    window.clearInterval(state.liveTimer);
    state.liveTimer = window.setInterval(() => updateLiveSignals("pulse"), 4500);
  }

  function updateLiveSignals(mode) {
    const payload = serializeTrip();
    const signals = calculateSignals(payload);
    updateSignal(els.routeSignalScore, els.routeSignalBar, els.routeSignalText, signals.route.value, signals.route.text);
    updateSignal(els.paceSignalScore, els.paceSignalBar, els.paceSignalText, signals.pace.value, signals.pace.text);
    updateSignal(els.budgetSignalScore, els.budgetSignalBar, els.budgetSignalText, signals.budget.value, signals.budget.text);
    updateSignal(els.constraintSignalScore, els.constraintSignalBar, els.constraintSignalText, signals.constraint.value, signals.constraint.text);

    const destination = shortPlace(payload.destination || "your destination");
    const days = tripDays(payload);
    const verb = mode === "editing" ? "Rebalancing" : "Watching";
    els.dynamicHeadline.textContent = `${verb} ${destination}: ${days} day(s), ${payload.travelers || 1} traveler(s), ${payload.travelMode}`;
    els.liveClock.textContent = `Live check ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  }

  function calculateSignals(payload) {
    const route = state.route || {};
    const days = tripDays(payload);
    const constraints = payload.constraints.length;
    const distanceKm = Math.round((route.distanceMeters || 0) / 1000);
    const durationHours = (route.durationSeconds || 0) / 3600;
    const hasGoogleRoute = route.source === "google-routes";
    const hasPlaceIds = Boolean(payload.originPlaceId && payload.destinationPlaceId);
    const avoidCount = [payload.avoidTolls, payload.avoidHighways, payload.avoidFerries].filter(Boolean).length;

    const routeScore = clamp((hasGoogleRoute ? 88 : 62) + (hasPlaceIds ? 7 : 0) - (distanceKm > 600 ? 7 : 0) - avoidCount * 3, 35, 98);
    const paceScore = clamp(88 - (payload.pace === "packed" ? constraints * 5 : constraints * 2) - (days <= 2 ? 8 : 0) + (payload.pace === "relaxed" ? 4 : 0), 38, 96);
    const budgetPressure = clamp((payload.budget === "lean" ? 68 : payload.budget === "premium" ? 34 : 48) + days * 3 + Number(payload.travelers || 1) * 2 + (durationHours > 8 ? 7 : 0), 18, 94);
    const constraintLoad = clamp(18 + constraints * 13 + avoidCount * 9 + (payload.optimizeStops ? -6 : 6), 10, 96);

    return {
      route: {
        value: routeScore,
        text: hasGoogleRoute
          ? `${route.distanceText || "Route"} with live Google traffic signal.`
          : `${route.distanceText || "Demo route"} until Google Routes key is configured.`,
      },
      pace: {
        value: paceScore,
        text: paceScore > 74 ? "Pace is healthy with room for buffers." : "Pace is tight; keep backup stops flexible.",
      },
      budget: {
        value: budgetPressure,
        text: budgetPressure > 70 ? "High pressure; prioritize bookings and local transfers." : "Budget pressure is manageable for this profile.",
      },
      constraint: {
        value: constraintLoad,
        text: constraintLoad > 70 ? "Many constraints active; plan keeps more recovery space." : "Constraints are light enough for flexible routing.",
      },
    };
  }

  function updateSignal(scoreEl, barEl, textEl, value, text) {
    scoreEl.textContent = `${Math.round(value)}%`;
    barEl.style.width = `${Math.round(value)}%`;
    textEl.textContent = text;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function applyDefaultDates() {
    const startInput = $("#startDate");
    const endInput = $("#endDate");
    if (startInput.value && endInput.value) {
      return;
    }
    const start = new Date();
    start.setDate(start.getDate() + 14);
    const end = new Date(start);
    end.setDate(start.getDate() + 3);
    startInput.value = toInputDate(start);
    endInput.value = toInputDate(end);
  }

  function toInputDate(date) {
    const offset = date.getTimezoneOffset();
    const local = new Date(date.getTime() - offset * 60000);
    return local.toISOString().slice(0, 10);
  }

  function serializeTrip() {
    const data = new FormData(form);
    return {
      origin: textValue("origin"),
      originPlaceId: textValue("originPlaceId"),
      destination: textValue("destination"),
      destinationPlaceId: textValue("destinationPlaceId"),
      startDate: textValue("startDate"),
      endDate: textValue("endDate"),
      travelers: Number(textValue("travelers") || 1),
      budget: textValue("budget"),
      pace: textValue("pace"),
      travelMode: textValue("travelMode"),
      interests: data.getAll("interests").map(String),
      constraints: splitNotes(textValue("constraints")),
      notes: textValue("notes"),
      avoidTolls: $("#avoidTolls").checked,
      avoidHighways: $("#avoidHighways").checked,
      avoidFerries: $("#avoidFerries").checked,
      optimizeStops: $("#optimizeStops").checked,
      route: state.route || {},
    };
  }

  function textValue(id) {
    return ($("#" + id).value || "").trim();
  }

  function splitNotes(value) {
    return value
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean)
      .slice(0, 12);
  }

  async function handlePlanSubmit(event) {
    event.preventDefault();
    setBusy(true);
    setRefreshing(true);
    try {
      await requestRoute({ silent: true });
      const payload = serializeTrip();
      payload.route = state.route || {};
      const envelope = await postJson("/api/plan", payload);
      renderPlanEnvelope(envelope);
      showWarnings(envelope.warnings);
      showToast(envelope.source === "gemini" ? "Gemini itinerary ready" : "Demo itinerary ready");
    } catch (error) {
      const fallback = { source: "demo", generatedAt: new Date().toISOString(), plan: clientFallbackPlan(serializeTrip()), warnings: [error.message] };
      renderPlanEnvelope(fallback);
      showToast("Planner fallback is active");
    } finally {
      setBusy(false);
      setRefreshing(false);
    }
  }

  async function requestRoute(options) {
    const silent = Boolean(options && options.silent);
    const payload = serializeTrip();
    updateRouteTitle();
    if (!payload.origin || !payload.destination) {
      return null;
    }

    if (!silent) {
      els.routeButton.disabled = true;
    }
    setRefreshing(true);

    try {
      const response = await postJson("/api/routes", payload);
      renderRoute(response.route);
      showWarnings(response.warnings);
      if (state.mapsLoaded) {
        requestGoogleDirections(payload);
      }
      if (!silent) {
        showToast(response.route.source === "google-routes" ? "Live route refreshed" : "Demo route refreshed");
      }
      return response.route;
    } catch (error) {
      const route = clientFallbackRoute(payload);
      renderRoute(route);
      if (!silent) {
        showToast("Route fallback is active");
      }
      return route;
    } finally {
      els.routeButton.disabled = false;
      setRefreshing(false);
    }
  }

  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Request failed");
    }
    return payload;
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Request failed");
    }
    return payload;
  }

  function renderRoute(route) {
    state.route = route;
    els.distanceMetric.textContent = route.distanceText || "Unavailable";
    els.durationMetric.textContent = route.durationText || "Unavailable";
    els.trafficMetric.textContent = route.trafficText || "Unavailable";
    els.sourceMetric.textContent = route.source === "google-routes" ? "Google" : "Demo";
    els.updatedAt.textContent = route.fetchedAt ? new Date(route.fetchedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Not refreshed";
    updateRouteTitle();
    els.originMarker.textContent = shortPlace(textValue("origin") || "Origin");
    els.destinationMarker.textContent = shortPlace(textValue("destination") || "Destination");
    updateLiveSignals("route");
  }

  function updateRouteTitle() {
    const origin = shortPlace(textValue("origin") || "Origin");
    const destination = shortPlace(textValue("destination") || "Destination");
    els.routeTitle.textContent = `${origin} to ${destination}`;
  }

  function shortPlace(value) {
    return value.split(",")[0].trim().slice(0, 24) || value;
  }

  function renderPlanEnvelope(envelope) {
    state.lastEnvelope = envelope;
    state.plan = envelope.plan;
    els.planSource.textContent = envelope.source === "gemini" ? "Gemini" : envelope.source === "live-preview" ? "Live preview" : "Demo mode";
    els.planTitle.textContent = envelope.plan.title || "Dynamic trip plan";
    els.planSummary.textContent = envelope.plan.summary || "Build a plan to see the current strategy.";
    renderItinerary(envelope.plan.dailyPlan || []);
    renderAdjustments(envelope.plan.liveAdjustments || []);
    renderBudget(envelope.plan.budget || {});
    renderRisks(envelope.plan.riskFlags || []);
    renderPacking(envelope.plan.packing || []);
    updateLiveSignals("plan");
  }

  function renderItinerary(days) {
    els.itineraryList.replaceChildren();
    if (!days.length) {
      els.itineraryList.append(emptyState("No itinerary yet"));
      return;
    }
    days.forEach((day) => {
      const card = document.createElement("article");
      card.className = "day-card";
      const title = document.createElement("h3");
      const badge = document.createElement("span");
      badge.className = "day-badge";
      badge.textContent = `D${day.day || ""}`;
      const theme = document.createElement("span");
      theme.textContent = day.theme || "Day plan";
      title.append(badge, theme);
      if (day.date) {
        const date = document.createElement("span");
        date.className = "time-pill";
        date.textContent = day.date;
        title.append(date);
      }
      card.append(title);
      card.append(planLine("Morning", day.morning));
      card.append(planLine("Afternoon", day.afternoon));
      card.append(planLine("Evening", day.evening));
      card.append(planLine("Route", day.routeNotes));
      card.append(planLine("Cost", day.estimatedCost));
      els.itineraryList.append(card);
    });
  }

  function planLine(label, value) {
    const paragraph = document.createElement("p");
    const strong = document.createElement("b");
    strong.textContent = `${label}: `;
    paragraph.append(strong, document.createTextNode(value || "Pending"));
    return paragraph;
  }

  function renderAdjustments(items) {
    els.adjustmentList.replaceChildren();
    if (!items.length) {
      els.adjustmentList.append(emptyState("No adjustments"));
      return;
    }
    items.forEach((item) => {
      const card = document.createElement("article");
      const priority = (item.priority || "medium").toLowerCase();
      card.className = `adjustment-card ${priority}`;
      const title = document.createElement("strong");
      title.textContent = item.signal || "Signal";
      const body = document.createElement("p");
      body.textContent = item.action || "Pending";
      card.append(title, body);
      els.adjustmentList.append(card);
    });
  }

  function renderBudget(budget) {
    els.budgetList.replaceChildren();
    const rows = [
      ["Currency", budget.currency],
      ["Lodging", budget.lodging],
      ["Food", budget.food],
      ["Transport", budget.localTransport],
      ["Experiences", budget.experiences],
    ];
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value || "Pending";
      els.budgetList.append(dt, dd);
    });
  }

  function renderRisks(items) {
    els.riskList.replaceChildren();
    if (!items.length) {
      els.riskList.append(emptyState("No risks"));
      return;
    }
    items.forEach((item) => {
      const row = document.createElement("article");
      row.className = "risk-item";
      const title = document.createElement("strong");
      title.textContent = item.risk || "Risk";
      const body = document.createElement("p");
      body.textContent = item.mitigation || "Pending";
      row.append(title, body);
      els.riskList.append(row);
    });
  }

  function renderPacking(items) {
    els.packingList.replaceChildren();
    if (!items.length) {
      els.packingList.append(emptyState("No packing items"));
      return;
    }
    items.forEach((item) => {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item;
      els.packingList.append(tag);
    });
  }

  function emptyState(message) {
    const div = document.createElement("div");
    div.className = "empty-state";
    div.textContent = message;
    return div;
  }

  function showWarnings(warnings) {
    if (Array.isArray(warnings) && warnings.length) {
      showToast(warnings[0]);
    }
  }

  function showToast(message) {
    if (!message) {
      return;
    }
    window.clearTimeout(state.toastTimer);
    els.toast.textContent = message;
    els.toast.classList.add("show");
    state.toastTimer = window.setTimeout(() => els.toast.classList.remove("show"), 3400);
  }

  function setBusy(isBusy) {
    els.planButton.disabled = isBusy;
    els.routeButton.disabled = isBusy;
    els.planButton.textContent = isBusy ? "Planning..." : "Plan Trip";
  }

  function setRefreshing(isRefreshing) {
    els.workspace.classList.toggle("refreshing", isRefreshing);
  }

  function saveTrip() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(serializeTrip()));
    } catch (error) {
      return;
    }
  }

  function hydrateSavedTrip() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || localStorage.getItem("promptwasr-trip") || "null");
      if (!saved) {
        return;
      }
      setInput("origin", saved.origin);
      setInput("originPlaceId", saved.originPlaceId);
      setInput("destination", saved.destination);
      setInput("destinationPlaceId", saved.destinationPlaceId);
      setInput("startDate", saved.startDate);
      setInput("endDate", saved.endDate);
      setInput("travelers", saved.travelers);
      setInput("budget", saved.budget);
      setInput("pace", saved.pace);
      setInput("travelMode", saved.travelMode);
      setInput("constraints", Array.isArray(saved.constraints) ? saved.constraints.join(", ") : "");
      setInput("notes", saved.notes);
      ["avoidTolls", "avoidHighways", "avoidFerries", "optimizeStops"].forEach((key) => {
        if (typeof saved[key] === "boolean") {
          $("#" + key).checked = saved[key];
        }
      });
      document.querySelectorAll("input[name='interests']").forEach((checkbox) => {
        checkbox.checked = Array.isArray(saved.interests) && saved.interests.includes(checkbox.value);
      });
    } catch (error) {
      return;
    }
  }

  function setInput(id, value) {
    const input = $("#" + id);
    if (input && value !== undefined && value !== null) {
      input.value = value;
    }
  }

  function resetTrip() {
    localStorage.removeItem(storageKey);
    localStorage.removeItem("promptwasr-trip");
    form.reset();
    $("#origin").value = "Bengaluru, India";
    $("#destination").value = "Goa, India";
    $("#travelers").value = "2";
    $("#constraints").value = "vegetarian meals, avoid very late nights, keep one recovery buffer each day";
    $("#notes").value = "Prefer clean beaches, scenic cafes, and safe evening transfers.";
    applyDefaultDates();
    renderRoute(clientFallbackRoute(serializeTrip()));
    renderPlanEnvelope({ source: "demo", generatedAt: new Date().toISOString(), plan: clientFallbackPlan(serializeTrip()), warnings: [] });
    updateLiveSignals("ready");
    showToast("Trip reset");
  }

  function exportPlan() {
    const payload = {
      trip: serializeTrip(),
      route: state.route,
      plan: state.plan,
      exportedAt: new Date().toISOString(),
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "travel-plan.json";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast("Export ready");
  }

  function wireAutocomplete(inputId, hiddenId, containerId) {
    const input = $("#" + inputId);
    const hidden = $("#" + hiddenId);
    const container = $("#" + containerId);
    let timer = null;
    input.addEventListener("input", () => {
      hidden.value = "";
      window.clearTimeout(timer);
      const query = input.value.trim();
      if (query.length < 2) {
        closeSuggestions(container);
        return;
      }
      timer = window.setTimeout(async () => {
        try {
          const response = await getJson(`/api/places?q=${encodeURIComponent(query)}`);
          renderSuggestions(response.suggestions || [], input, hidden, container);
        } catch (error) {
          closeSuggestions(container);
        }
      }, 220);
    });
    input.addEventListener("blur", () => window.setTimeout(() => closeSuggestions(container), 160));
  }

  function renderSuggestions(suggestions, input, hidden, container) {
    container.replaceChildren();
    if (!suggestions.length) {
      closeSuggestions(container);
      return;
    }
    suggestions.forEach((suggestion) => {
      const button = document.createElement("button");
      button.className = "suggestion-option";
      button.type = "button";
      const main = document.createElement("strong");
      main.textContent = suggestion.mainText || suggestion.text;
      const secondary = document.createElement("span");
      secondary.textContent = suggestion.secondaryText || suggestion.type || "";
      button.append(main, secondary);
      button.addEventListener("click", () => {
        input.value = suggestion.text || suggestion.mainText;
        hidden.value = suggestion.placeId || "";
        closeSuggestions(container);
        saveTrip();
        updateRouteTitle();
        updateLiveSignals("editing");
        schedulePlanPreview();
        scheduleRouteRefresh();
      });
      container.append(button);
    });
    container.classList.add("open");
  }

  function closeSuggestions(container) {
    container.classList.remove("open");
    container.replaceChildren();
  }

  function clientFallbackRoute(payload) {
    const seed = Array.from(`${payload.origin}->${payload.destination}`).reduce((sum, char) => sum + char.charCodeAt(0), 0);
    const modeFactor = { DRIVE: 72, TWO_WHEELER: 58, TRANSIT: 46, BICYCLE: 16, WALK: 5 }[payload.travelMode] || 72;
    const distanceKm = 35 + (seed % 900);
    const baseSeconds = Math.round((distanceKm / modeFactor) * 3600);
    const trafficSeconds = ["DRIVE", "TWO_WHEELER"].includes(payload.travelMode) ? Math.round(baseSeconds * 0.11) : 0;
    return {
      source: "demo-route",
      fetchedAt: new Date().toISOString(),
      distanceMeters: distanceKm * 1000,
      distanceText: `${distanceKm} km estimate`,
      durationSeconds: baseSeconds + trafficSeconds,
      durationText: formatDuration(baseSeconds + trafficSeconds),
      trafficDelaySeconds: trafficSeconds,
      trafficText: "Demo traffic estimate",
      warnings: ["Demo route"],
    };
  }

  function clientFallbackPlan(payload) {
    const days = tripDays(payload);
    const interests = payload.interests.length ? payload.interests : ["local food", "culture", "signature sights"];
    const start = payload.startDate ? new Date(payload.startDate + "T00:00:00") : null;
    const dailyPlan = Array.from({ length: days }, (_, index) => {
      const interest = interests[index % interests.length];
      const date = start ? toInputDate(new Date(start.getTime() + index * 86400000)) : "";
      return {
        day: index + 1,
        date,
        theme: `${shortPlace(payload.destination)} through ${interest}`,
        morning: `Start with a compact arrival window from ${shortPlace(payload.origin)} and place the first anchor near the route endpoint.`,
        afternoon: `Use the main ${interest} block with one nearby backup stop and a protected meal window.`,
        evening: "Keep dinner close to the final activity and preserve a safe transfer buffer.",
        routeNotes: state.route ? state.route.trafficText : "Refresh route before departure.",
        estimatedCost: `${titleCase(payload.budget)} tier for ${payload.travelers} traveler(s).`,
      };
    });
    return {
      title: `${shortPlace(payload.destination)} dynamic travel plan`,
      summary: `${days} days shaped around ${payload.pace} pacing, constraints, and route signals.`,
      dailyPlan,
      recommendedStops: [
        { name: `${shortPlace(payload.destination)} arrival hub`, reason: "Absorbs route uncertainty.", window: "Arrival day" },
        { name: `${shortPlace(payload.destination)} food district`, reason: "Flexible meal options.", window: "Evening" },
      ],
      budget: {
        currency: "local",
        lodging: "Stay near the first two anchors.",
        food: "Mix reservations with flexible local stops.",
        localTransport: "Refresh route duration before each transfer.",
        experiences: "Pre-book limited-capacity activities.",
      },
      constraintsHandled: payload.constraints.length ? payload.constraints : ["Transfer buffers protected"],
      liveAdjustments: [
        { signal: "Traffic and route duration", action: state.route ? state.route.trafficText : "Refresh route before departure.", priority: "high" },
        { signal: "Weather and operating hours", action: "Check same-day conditions for outdoor and ticketed stops.", priority: "medium" },
        { signal: "Group pace", action: `Keep the plan ${payload.pace} and drop flexible stops first.`, priority: "medium" },
      ],
      riskFlags: [
        { risk: "Live APIs missing", mitigation: "Configure Gemini and Google Maps keys in .env." },
        { risk: "Ambiguous locations", mitigation: "Use Google Places suggestions when available." },
      ],
      packing: ["Comfortable shoes", "Weather layer", "Portable charger", "Identity documents", "Booking confirmations"],
    };
  }

  function tripDays(payload) {
    if (!payload.startDate || !payload.endDate) {
      return 4;
    }
    const start = new Date(payload.startDate + "T00:00:00");
    const end = new Date(payload.endDate + "T00:00:00");
    const diff = Math.round((end - start) / 86400000) + 1;
    return Math.min(21, Math.max(1, Number.isFinite(diff) ? diff : 4));
  }

  function formatDuration(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    if (hours && mins) {
      return `${hours} hr ${mins} min`;
    }
    if (hours) {
      return `${hours} hr`;
    }
    return `${mins} min`;
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/[-_]/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  async function loadGoogleMaps(key) {
    if (window.google && window.google.maps) {
      initializeGoogleMap();
      return;
    }
    await new Promise((resolve, reject) => {
      const callbackName = "initTripPulseMap";
      window[callbackName] = () => {
        delete window[callbackName];
        resolve();
      };
      const script = document.createElement("script");
      script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&libraries=places&callback=${callbackName}`;
      script.async = true;
      script.defer = true;
      script.onerror = () => reject(new Error("Google Maps failed to load"));
      document.head.append(script);
    });
    initializeGoogleMap();
  }

  function initializeGoogleMap() {
    if (!window.google || !window.google.maps) {
      return;
    }
    const mapElement = $("#map");
    state.map = new google.maps.Map(mapElement, {
      center: { lat: 15.2993, lng: 74.124 },
      zoom: 7,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: true,
    });
    state.directionsService = new google.maps.DirectionsService();
    state.directionsRenderer = new google.maps.DirectionsRenderer({ suppressMarkers: false, preserveViewport: false });
    state.directionsRenderer.setMap(state.map);
    state.mapsLoaded = true;
    setupGoogleAutocomplete("origin", "originPlaceId");
    setupGoogleAutocomplete("destination", "destinationPlaceId");
    requestGoogleDirections(serializeTrip());
  }

  function setupGoogleAutocomplete(inputId, hiddenId) {
    if (!google.maps.places) {
      return;
    }
    const input = $("#" + inputId);
    const hidden = $("#" + hiddenId);
    const autocomplete = new google.maps.places.Autocomplete(input, { fields: ["place_id", "formatted_address", "name"] });
    autocomplete.addListener("place_changed", () => {
      const place = autocomplete.getPlace();
      hidden.value = place.place_id || "";
      input.value = place.formatted_address || place.name || input.value;
      saveTrip();
      updateRouteTitle();
      updateLiveSignals("editing");
      schedulePlanPreview();
      scheduleRouteRefresh();
    });
  }

  function requestGoogleDirections(payload) {
    if (!state.directionsService || !state.directionsRenderer || !payload.origin || !payload.destination) {
      return;
    }
    const travelMode = {
      DRIVE: google.maps.TravelMode.DRIVING,
      TRANSIT: google.maps.TravelMode.TRANSIT,
      WALK: google.maps.TravelMode.WALKING,
      BICYCLE: google.maps.TravelMode.BICYCLING,
      TWO_WHEELER: google.maps.TravelMode.DRIVING,
    }[payload.travelMode] || google.maps.TravelMode.DRIVING;

    state.directionsService.route(
      {
        origin: payload.originPlaceId ? { placeId: payload.originPlaceId } : payload.origin,
        destination: payload.destinationPlaceId ? { placeId: payload.destinationPlaceId } : payload.destination,
        travelMode,
        avoidTolls: payload.avoidTolls,
        avoidHighways: payload.avoidHighways,
        avoidFerries: payload.avoidFerries,
      },
      (response, status) => {
        if (status === "OK") {
          state.directionsRenderer.setDirections(response);
        }
      }
    );
  }
})();
