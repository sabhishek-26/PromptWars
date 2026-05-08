# TripPulse

Live Travel Planning & Experience Engine in `promptwasr`: Python API proxy, responsive frontend, Google Maps hooks, Gemini itinerary generation, live signal panels, and deterministic demo fallbacks when keys are not configured.

## Run

```powershell
cd d:\Preparation\Portfolio\promptwasr
copy .env.example .env
python server.py
```

Open `http://127.0.0.1:8080`.

## Google Setup

Use separate keys:

- `GEMINI_API_KEY`: server-side Gemini `generateContent` planning.
- `GOOGLE_MAPS_SERVER_KEY`: server-side Routes API and Places Autocomplete web service.
- `GOOGLE_MAPS_BROWSER_KEY`: browser-side Maps JavaScript API. Restrict this by HTTP referrer.

Enable these Google APIs in your project:

- Gemini API
- Maps JavaScript API
- Routes API
- Places API

The app still runs without keys. In that mode it uses demo routes, demo place suggestions, and a deterministic itinerary generator so the UI remains usable.

## API

- `GET /api/config`: returns enabled integrations and the browser Maps key.
- `GET /api/places?q=...`: returns Google Places predictions or demo suggestions.
- `POST /api/routes`: returns Google Routes traffic-aware route metrics or a demo estimate.
- `POST /api/plan`: returns a Gemini JSON itinerary or deterministic fallback plan.

## Notes

The requested `instructions.md` or `intructions.md` file was not present in this repository, so this implementation follows a clean standalone structure. The server uses only the Python standard library to keep setup fast on this machine.

## Test

```powershell
cd d:\Preparation\Portfolio\promptwasr
python -m unittest discover -s tests
```
