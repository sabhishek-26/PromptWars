from __future__ import annotations

import datetime as dt
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
MAX_BODY_BYTES = 128 * 1024
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class AppError(Exception):
    def __init__(self, status: int, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any, max_len: int = 240) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_len]


def clean_list(values: Any, max_items: int = 12, max_len: int = 80) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values[:max_items]:
        text = clean_text(item, max_len)
        if text:
            result.append(text)
    return result


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_travel_mode(value: Any) -> str:
    mode = clean_text(value, 32).upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "CAR": "DRIVE",
        "DRIVING": "DRIVE",
        "AUTO": "DRIVE",
        "WALK": "WALK",
        "WALKING": "WALK",
        "BIKE": "BICYCLE",
        "CYCLING": "BICYCLE",
        "TRANSIT": "TRANSIT",
        "TRAIN": "TRANSIT",
        "TWO_WHEELER": "TWO_WHEELER",
        "SCOOTER": "TWO_WHEELER",
        "MOTORBIKE": "TWO_WHEELER",
    }
    return aliases.get(mode, mode if mode in {"DRIVE", "WALK", "BICYCLE", "TRANSIT", "TWO_WHEELER"} else "DRIVE")


def parse_iso_date(value: Any) -> dt.date | None:
    text = clean_text(value, 32)
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def trip_length_days(payload: dict[str, Any]) -> int:
    start = parse_iso_date(payload.get("startDate"))
    end = parse_iso_date(payload.get("endDate"))
    if start and end and end >= start:
        return max(1, min(21, (end - start).days + 1))
    return max(1, min(10, int(payload.get("days") or 4)))


def validate_trip_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError(400, "Request body must be a JSON object.")

    origin = clean_text(payload.get("origin"), 180)
    destination = clean_text(payload.get("destination"), 180)
    if len(origin) < 2 or len(destination) < 2:
        raise AppError(400, "Origin and destination are required.")

    try:
        travelers = int(payload.get("travelers") or 1)
    except (TypeError, ValueError) as exc:
        raise AppError(400, "Travelers must be a valid number.") from exc
    if travelers < 1 or travelers > 24:
        raise AppError(400, "Travelers must be between 1 and 24.")

    normalized = {
        "origin": origin,
        "originPlaceId": clean_text(payload.get("originPlaceId"), 120),
        "destination": destination,
        "destinationPlaceId": clean_text(payload.get("destinationPlaceId"), 120),
        "startDate": clean_text(payload.get("startDate"), 32),
        "endDate": clean_text(payload.get("endDate"), 32),
        "travelers": travelers,
        "budget": clean_text(payload.get("budget") or "moderate", 40),
        "pace": clean_text(payload.get("pace") or "balanced", 40),
        "travelMode": normalize_travel_mode(payload.get("travelMode")),
        "interests": clean_list(payload.get("interests"), 12, 80),
        "constraints": clean_list(payload.get("constraints"), 12, 140),
        "notes": clean_text(payload.get("notes"), 900),
        "avoidTolls": coerce_bool(payload.get("avoidTolls")),
        "avoidHighways": coerce_bool(payload.get("avoidHighways")),
        "avoidFerries": coerce_bool(payload.get("avoidFerries")),
        "optimizeStops": coerce_bool(payload.get("optimizeStops")),
        "route": payload.get("route") if isinstance(payload.get("route"), dict) else {},
    }
    normalized["days"] = trip_length_days(normalized)
    return normalized


def http_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise AppError(exc.code, "Google API request failed.", {"response": error_text[:1200]}) from exc
    except urllib.error.URLError as exc:
        raise AppError(502, "Google API is unreachable from this server.", {"reason": str(exc.reason)}) from exc
    except TimeoutError as exc:
        raise AppError(504, "Google API request timed out.") from exc


def extract_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def itinerary_schema() -> dict[str, Any]:
    string_array = {"type": "ARRAY", "items": {"type": "STRING"}}
    return {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "dailyPlan": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "day": {"type": "INTEGER"},
                        "date": {"type": "STRING"},
                        "theme": {"type": "STRING"},
                        "morning": {"type": "STRING"},
                        "afternoon": {"type": "STRING"},
                        "evening": {"type": "STRING"},
                        "routeNotes": {"type": "STRING"},
                        "estimatedCost": {"type": "STRING"},
                    },
                    "required": ["day", "theme", "morning", "afternoon", "evening", "routeNotes", "estimatedCost"],
                },
            },
            "recommendedStops": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                        "window": {"type": "STRING"},
                    },
                    "required": ["name", "reason", "window"],
                },
            },
            "budget": {
                "type": "OBJECT",
                "properties": {
                    "currency": {"type": "STRING"},
                    "lodging": {"type": "STRING"},
                    "food": {"type": "STRING"},
                    "localTransport": {"type": "STRING"},
                    "experiences": {"type": "STRING"},
                },
            },
            "constraintsHandled": string_array,
            "liveAdjustments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "signal": {"type": "STRING"},
                        "action": {"type": "STRING"},
                        "priority": {"type": "STRING"},
                    },
                    "required": ["signal", "action", "priority"],
                },
            },
            "riskFlags": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "risk": {"type": "STRING"},
                        "mitigation": {"type": "STRING"},
                    },
                    "required": ["risk", "mitigation"],
                },
            },
            "packing": string_array,
        },
        "required": ["title", "summary", "dailyPlan", "recommendedStops", "budget", "constraintsHandled", "liveAdjustments", "riskFlags", "packing"],
    }


def build_gemini_prompt(payload: dict[str, Any]) -> str:
    route = payload.get("route") or {}
    route_summary = {
        "source": clean_text(route.get("source"), 80),
        "distance": clean_text(route.get("distanceText"), 80),
        "duration": clean_text(route.get("durationText"), 80),
        "traffic": clean_text(route.get("trafficText"), 120),
    }
    return (
        "You are a senior travel operations planner creating a dynamic trip plan. "
        "Use realistic pacing, avoid overstuffed days, protect constraint buffers, and never invent live traffic or opening-hour data. "
        "When a live signal is unavailable, say what should be checked before departure. "
        "Return only valid JSON for the requested schema.\n\n"
        f"Current date: 2026-05-08.\n"
        f"Trip request: {json.dumps(payload, ensure_ascii=False)}\n"
        f"Route signal: {json.dumps(route_summary, ensure_ascii=False)}\n"
    )


def call_gemini(payload: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise AppError(424, "GEMINI_API_KEY is not configured.")

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip().removeprefix("models/")
    model_path = urllib.parse.quote(model, safe="-_.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}:generateContent?key={urllib.parse.quote(key)}"
    request_body = {
        "contents": [{"role": "user", "parts": [{"text": build_gemini_prompt(payload)}]}],
        "generationConfig": {
            "temperature": 0.35,
            "topP": 0.9,
            "responseMimeType": "application/json",
            "responseSchema": itinerary_schema(),
        },
    }
    response = http_json(url, request_body, timeout=30)
    candidates = response.get("candidates") or []
    if not candidates:
        raise AppError(502, "Gemini returned no candidates.", {"response": response})
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text.strip():
        raise AppError(502, "Gemini returned an empty response.", {"response": response})
    plan = extract_json_text(text)
    return normalize_plan(plan, payload)


def normalize_plan(plan: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    days = payload["days"]
    daily = plan.get("dailyPlan") if isinstance(plan.get("dailyPlan"), list) else []
    if not daily:
        daily = fallback_plan(payload)["dailyPlan"]
    normalized_days = []
    for index, item in enumerate(daily[:days], start=1):
        if not isinstance(item, dict):
            continue
        normalized_days.append(
            {
                "day": int(item.get("day") or index),
                "date": clean_text(item.get("date"), 32),
                "theme": clean_text(item.get("theme"), 120),
                "morning": clean_text(item.get("morning"), 420),
                "afternoon": clean_text(item.get("afternoon"), 420),
                "evening": clean_text(item.get("evening"), 420),
                "routeNotes": clean_text(item.get("routeNotes"), 260),
                "estimatedCost": clean_text(item.get("estimatedCost"), 120),
            }
        )

    if len(normalized_days) < days:
        normalized_days.extend(fallback_plan(payload)["dailyPlan"][len(normalized_days) : days])

    budget = plan.get("budget") if isinstance(plan.get("budget"), dict) else {}
    return {
        "title": clean_text(plan.get("title") or f"{payload['destination']} travel plan", 120),
        "summary": clean_text(plan.get("summary") or "Balanced itinerary generated from the trip profile.", 420),
        "dailyPlan": normalized_days,
        "recommendedStops": normalize_object_list(plan.get("recommendedStops"), ["name", "reason", "window"], 8),
        "budget": {
            "currency": clean_text(budget.get("currency") or "local", 24),
            "lodging": clean_text(budget.get("lodging") or "Match lodging tier to the selected budget.", 160),
            "food": clean_text(budget.get("food") or "Reserve flexible meal budget near anchor activities.", 160),
            "localTransport": clean_text(budget.get("localTransport") or "Use the live route estimate before booking transfers.", 160),
            "experiences": clean_text(budget.get("experiences") or "Pre-book high-demand experiences first.", 160),
        },
        "constraintsHandled": normalize_string_list(plan.get("constraintsHandled"), payload.get("constraints") or []),
        "liveAdjustments": normalize_object_list(plan.get("liveAdjustments"), ["signal", "action", "priority"], 8),
        "riskFlags": normalize_object_list(plan.get("riskFlags"), ["risk", "mitigation"], 8),
        "packing": normalize_string_list(plan.get("packing"), ["Comfortable shoes", "Weather layer", "Identity documents"]),
    }


def normalize_string_list(value: Any, fallback: list[str], limit: int = 10) -> list[str]:
    items = clean_list(value, limit, 160)
    return items if items else fallback[:limit]


def normalize_object_list(value: Any, fields: list[str], limit: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        normalized = {field: clean_text(item.get(field), 220) for field in fields}
        if any(normalized.values()):
            result.append(normalized)
    return result


def fallback_plan(payload: dict[str, Any]) -> dict[str, Any]:
    destination = payload["destination"]
    origin = payload["origin"]
    interests = payload["interests"] or ["local food", "culture", "signature sights"]
    constraints = payload["constraints"] or ["Protect transfer buffers and avoid last-minute scheduling."]
    start = parse_iso_date(payload.get("startDate"))
    days = payload["days"]
    pace_map = {
        "relaxed": "two anchor activities with long buffers",
        "balanced": "two to three anchors with flexible meals",
        "packed": "three anchors with pre-booked transfers",
    }
    pace = pace_map.get(payload.get("pace", "").lower(), "two to three anchors with flexible meals")
    route = payload.get("route") or {}
    route_text = clean_text(route.get("durationText") or route.get("trafficText") or "Confirm live traffic before departure.", 180)

    daily = []
    for index in range(days):
        date_text = (start + dt.timedelta(days=index)).isoformat() if start else ""
        interest = interests[index % len(interests)]
        daily.append(
            {
                "day": index + 1,
                "date": date_text,
                "theme": f"{destination} through {interest}",
                "morning": f"Start with a low-friction arrival window from {origin}, then handle check-in, passes, and first local orientation.",
                "afternoon": f"Plan the main {interest} block near a compact neighborhood so the group avoids cross-town backtracking.",
                "evening": f"Keep dinner close to the final afternoon area and hold a backup indoor option for delays or weather changes.",
                "routeNotes": route_text if index == 0 else f"Keep day {index + 1} transfers compact and preserve at least one recovery buffer.",
                "estimatedCost": f"{payload['budget'].title()} tier for {payload['travelers']} traveler(s).",
            }
        )

    stops = [
        {"name": f"{destination} arrival hub", "reason": "Best place to absorb transport uncertainty.", "window": "Arrival day"},
        {"name": f"{destination} food district", "reason": "Efficient dinner fallback with multiple price points.", "window": "Evening"},
        {"name": f"{destination} culture anchor", "reason": "Matches selected interests and creates a clear day theme.", "window": "Morning"},
    ]

    return {
        "title": f"{destination} dynamic travel plan",
        "summary": f"A {days}-day {payload['pace']} plan from {origin} to {destination} with live-route hooks and constraint buffers.",
        "dailyPlan": daily,
        "recommendedStops": stops,
        "budget": {
            "currency": "local",
            "lodging": "Book near the first two anchors to reduce transfer risk.",
            "food": "Mix reserved dinners with flexible cafes near route endpoints.",
            "localTransport": "Refresh live route duration before each transfer window.",
            "experiences": "Pre-book limited-capacity activities and leave backup slots open.",
        },
        "constraintsHandled": constraints,
        "liveAdjustments": [
            {"signal": "Traffic and route duration", "action": "Refresh the Google route before departure and move flexible stops later if delayed.", "priority": "high"},
            {"signal": "Weather and operating hours", "action": "Confirm same-day conditions for outdoor and ticketed stops.", "priority": "medium"},
            {"signal": "Group pace", "action": f"Use {pace}; drop the lowest-priority stop when energy dips.", "priority": "medium"},
        ],
        "riskFlags": [
            {"risk": "Live APIs are not configured on this server.", "mitigation": "Add Gemini and Google Maps keys in .env for real-time planning signals."},
            {"risk": "Ambiguous place names can create bad routes.", "mitigation": "Use Google Places suggestions or place IDs for origin and destination."},
        ],
        "packing": ["Comfortable walking shoes", "Weather layer", "Portable charger", "Identity documents", "Booking confirmations"],
    }


def google_waypoint(text: str, place_id: str = "") -> dict[str, str]:
    if place_id:
        return {"placeId": place_id}
    return {"address": text}


def call_routes(payload: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("GOOGLE_MAPS_SERVER_KEY", "").strip()
    if not key:
        raise AppError(424, "GOOGLE_MAPS_SERVER_KEY is not configured.")

    mode = payload["travelMode"]
    body: dict[str, Any] = {
        "origin": google_waypoint(payload["origin"], payload.get("originPlaceId", "")),
        "destination": google_waypoint(payload["destination"], payload.get("destinationPlaceId", "")),
        "travelMode": mode,
        "computeAlternativeRoutes": False,
        "languageCode": "en-US",
        "units": "METRIC",
    }
    if mode in {"DRIVE", "TWO_WHEELER"}:
        body["routingPreference"] = "TRAFFIC_AWARE"
        body["routeModifiers"] = {
            "avoidTolls": payload["avoidTolls"],
            "avoidHighways": payload["avoidHighways"],
            "avoidFerries": payload["avoidFerries"],
        }

    headers = {
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters,routes.polyline.encodedPolyline,routes.warnings",
    }
    response = http_json("https://routes.googleapis.com/directions/v2:computeRoutes", body, headers=headers, timeout=20)
    routes = response.get("routes") or []
    if not routes:
        raise AppError(404, "Google Routes returned no route.", {"response": response})

    route = routes[0]
    duration_seconds = parse_google_duration(route.get("duration"))
    static_seconds = parse_google_duration(route.get("staticDuration"))
    traffic_delta = max(0, duration_seconds - static_seconds) if duration_seconds and static_seconds else 0
    distance_meters = int(route.get("distanceMeters") or 0)
    return {
        "source": "google-routes",
        "fetchedAt": utc_now_iso(),
        "distanceMeters": distance_meters,
        "distanceText": format_distance(distance_meters),
        "durationSeconds": duration_seconds,
        "durationText": format_duration(duration_seconds),
        "staticDurationSeconds": static_seconds,
        "trafficDelaySeconds": traffic_delta,
        "trafficText": format_traffic(traffic_delta),
        "encodedPolyline": ((route.get("polyline") or {}).get("encodedPolyline") or ""),
        "warnings": route.get("warnings") or [],
    }


def parse_google_duration(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    match = re.match(r"^(\d+(?:\.\d+)?)s$", value.strip())
    if not match:
        return 0
    return int(float(match.group(1)))


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "Unavailable"
    minutes = round(seconds / 60)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    if hours:
        return f"{hours} hr"
    return f"{mins} min"


def format_distance(meters: int) -> str:
    if meters <= 0:
        return "Unavailable"
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{meters} m"


def format_traffic(seconds: int) -> str:
    if seconds <= 0:
        return "No live delay reported"
    return f"{format_duration(seconds)} live delay"


def fallback_route(payload: dict[str, Any]) -> dict[str, Any]:
    seed = sum(ord(char) for char in f"{payload['origin']}->{payload['destination']}")
    mode_factor = {"DRIVE": 72, "TWO_WHEELER": 58, "TRANSIT": 46, "BICYCLE": 16, "WALK": 5}.get(payload["travelMode"], 72)
    distance_km = 35 + (seed % 900)
    duration_seconds = int((distance_km / mode_factor) * 3600)
    traffic_seconds = int(duration_seconds * (0.08 + (seed % 8) / 100)) if payload["travelMode"] in {"DRIVE", "TWO_WHEELER"} else 0
    return {
        "source": "demo-route",
        "fetchedAt": utc_now_iso(),
        "distanceMeters": distance_km * 1000,
        "distanceText": f"{distance_km} km estimate",
        "durationSeconds": duration_seconds + traffic_seconds,
        "durationText": format_duration(duration_seconds + traffic_seconds),
        "staticDurationSeconds": duration_seconds,
        "trafficDelaySeconds": traffic_seconds,
        "trafficText": "Demo traffic estimate; configure Google Routes for live data.",
        "encodedPolyline": "",
        "warnings": ["Demo route shown because Google Routes is not configured or unavailable."],
    }


def call_places(query: str) -> dict[str, Any]:
    key = os.getenv("GOOGLE_MAPS_SERVER_KEY", "").strip()
    if not key:
        return {"source": "demo-places", "suggestions": fallback_places(query)}

    body = {
        "input": query,
        "includeQueryPredictions": True,
        "languageCode": os.getenv("GOOGLE_PLACES_LANGUAGE", "en"),
    }
    region = os.getenv("GOOGLE_PLACES_REGION", "").strip().lower()
    if region:
        body["includedRegionCodes"] = [region]

    headers = {
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "suggestions.placePrediction.placeId,"
            "suggestions.placePrediction.text.text,"
            "suggestions.placePrediction.structuredFormat.mainText.text,"
            "suggestions.placePrediction.structuredFormat.secondaryText.text,"
            "suggestions.queryPrediction.text.text"
        ),
    }
    response = http_json("https://places.googleapis.com/v1/places:autocomplete", body, headers=headers, timeout=12)
    suggestions = []
    for item in response.get("suggestions") or []:
        place = item.get("placePrediction")
        query_prediction = item.get("queryPrediction")
        if place:
            structured = place.get("structuredFormat") or {}
            suggestions.append(
                {
                    "type": "place",
                    "placeId": clean_text(place.get("placeId"), 120),
                    "text": clean_text((place.get("text") or {}).get("text"), 220),
                    "mainText": clean_text((structured.get("mainText") or {}).get("text"), 120),
                    "secondaryText": clean_text((structured.get("secondaryText") or {}).get("text"), 160),
                }
            )
        elif query_prediction:
            text = clean_text((query_prediction.get("text") or {}).get("text"), 220)
            suggestions.append({"type": "query", "placeId": "", "text": text, "mainText": text, "secondaryText": ""})
    return {"source": "google-places", "suggestions": suggestions[:5]}


def fallback_places(query: str) -> list[dict[str, str]]:
    samples = [
        "Goa, India",
        "Jaipur, Rajasthan, India",
        "Bengaluru, Karnataka, India",
        "New Delhi, India",
        "Mumbai, Maharashtra, India",
        "Singapore",
        "Dubai, United Arab Emirates",
        "Bali, Indonesia",
        "Tokyo, Japan",
        "Paris, France",
        "New York, NY, USA",
    ]
    lower = query.lower()
    matches = [item for item in samples if lower in item.lower()][:5]
    if not matches:
        matches = [query]
    return [{"type": "demo", "placeId": "", "text": item, "mainText": item, "secondaryText": "Demo suggestion"} for item in matches]


class TravelHandler(BaseHTTPRequestHandler):
    server_version = "TripPulse/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_common_headers("application/json")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/health":
                self.send_json({"ok": True, "time": utc_now_iso()})
                return
            if path == "/api/config":
                self.send_json(
                    {
                        "appName": "TripPulse Live Travel Planning Engine",
                        "googleMapsBrowserKey": os.getenv("GOOGLE_MAPS_BROWSER_KEY", "").strip(),
                        "features": {
                            "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
                            "routes": bool(os.getenv("GOOGLE_MAPS_SERVER_KEY", "").strip()),
                            "maps": bool(os.getenv("GOOGLE_MAPS_BROWSER_KEY", "").strip()),
                        },
                    }
                )
                return
            if path == "/api/places":
                query = clean_text(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0], 120)
                if len(query) < 2:
                    self.send_json({"source": "none", "suggestions": []})
                    return
                self.send_json(call_places(query))
                return
            self.serve_static(path)
        except AppError as exc:
            self.send_json({"error": exc.message, "details": exc.details}, status=exc.status)
        except Exception as exc:
            self.send_json({"error": "Unexpected server error.", "details": {"message": str(exc)}}, status=500)

    def do_POST(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            payload = self.read_json_body()
            if path == "/api/plan":
                trip = validate_trip_payload(payload)
                warnings = []
                try:
                    plan = call_gemini(trip)
                    source = "gemini"
                except AppError as exc:
                    plan = fallback_plan(trip)
                    source = "demo"
                    warnings.append(exc.message)
                self.send_json({"source": source, "generatedAt": utc_now_iso(), "plan": plan, "warnings": warnings})
                return
            if path == "/api/routes":
                trip = validate_trip_payload(payload)
                warnings = []
                try:
                    route = call_routes(trip)
                except AppError as exc:
                    route = fallback_route(trip)
                    warnings.append(exc.message)
                self.send_json({"route": route, "warnings": warnings})
                return
            raise AppError(404, "API endpoint not found.")
        except AppError as exc:
            self.send_json({"error": exc.message, "details": exc.details}, status=exc.status)
        except Exception as exc:
            self.send_json({"error": "Unexpected server error.", "details": {"message": str(exc)}}, status=500)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise AppError(413, "Request body is too large.")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppError(400, "Request body must be valid JSON.") from exc

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            target = PUBLIC_DIR / "index.html"
        else:
            relative = urllib.parse.unquote(path).lstrip("/")
            target = (PUBLIC_DIR / relative).resolve()
            if PUBLIC_DIR.resolve() not in target.parents and target != PUBLIC_DIR.resolve():
                raise AppError(403, "Forbidden path.")
            if target.is_dir():
                target = target / "index.html"

        if not target.exists() or not target.is_file():
            target = PUBLIC_DIR / "index.html"

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_common_headers(content_type)
        self.send_header("Cache-Control", "no-store" if target.name == "index.html" else "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_common_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "same-origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://maps.googleapis.com https://maps.gstatic.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data: https://maps.googleapis.com https://*.googleapis.com https://maps.gstatic.com https://*.gstatic.com; "
            "connect-src 'self' https://maps.googleapis.com https://*.googleapis.com https://maps.gstatic.com https://*.gstatic.com; "
            "font-src 'self' https://fonts.gstatic.com https://*.gstatic.com; "
            "frame-src https://www.google.com;",
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("APP_ENV", "development") != "test":
            super().log_message(fmt, *args)


def main() -> None:
    load_dotenv()
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("127.0.0.1", port), TravelHandler)
    print(f"TripPulse running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
