# Best Practices — setting up your home so Cooper works flawlessly

Cooper is only as good as what it can **see** and how clearly that's **named**. It doesn't crawl
your whole Home Assistant install — it reasons over the entities you **expose** to it, identifies
them by their **friendly names and aliases**, and uses their **areas, floors, and zones** to
understand "upstairs," "the office," or "at school." Get those four things right and Cooper feels
telepathic. Get them wrong and it guesses, or reports the coarse truth instead of the useful one.

This guide is the setup that makes the difference. None of it is Cooper-specific configuration —
it's good Home Assistant hygiene that Cooper happens to reward heavily.

---

## The one mental model

> **Cooper sees exposed entities, by name, with their live state — and nothing else.**

Every recommendation below follows from that sentence:

- Not exposed → Cooper doesn't know it exists.
- Exposed but cryptically named → Cooper can't reliably pick it out, or describes it wrong.
- Exposed and well-named but with no area/zone → Cooper can't answer "what's on downstairs?" or
  "who's at work?"

---

## 1. Exposure — show the useful, hide the noise

Cooper grounds on Home Assistant's built-in **Assist exposure**. Expose an entity to the
`conversation` assistant and Cooper can see and (if it's a controllable domain) act on it.

**Do:**
- Expose the things you'd actually ask or talk about: lights, climate, media players, covers,
  the locks/cameras you want it to use, presence/location sensors, key binary sensors (doors,
  motion), and scenes.
- Manage exposure in **Settings → Voice assistants → Expose** (bulk select by domain/area), or
  per entity under the entity's **Voice assistants** section.

**Don't:**
- Don't expose hundreds of diagnostic/config entities (signal strength, battery %, uptime,
  per-integration debug sensors). Noise dilutes Cooper's attention and slows grounding.
- Don't assume exposure is permanent. **Re-creating an entity (integration reload, device
  rename, re-add) can drop its exposure.** If Cooper suddenly "forgets" something, re-check that
  it's still exposed. (Verify the live exposure, not just the UI toggle, after big changes.)

**Rule of thumb:** if you'd never mention it out loud, don't expose it.

---

## 2. Naming & aliases — friendly names are Cooper's vocabulary

The friendly name **is** how the model refers to an entity. Names are the single highest-leverage
thing you can fix.

**Do:**
- Use plain, human names: *"Kitchen Lights," "Front Door Lock," "Driveway Camera," "Office
  Thermostat."*
- Make every exposed name **unambiguous**. If two entities can answer to the same word, Cooper may
  pick the less useful one.
  - ⚠️ **Name-collision trap:** if a *person* entity and a *location sensor* both read as the same
    name (e.g. both called "Alex"), Cooper anchors on the person (bare *home/away*) and never uses
    the sensor that actually holds the street/town. **Fix:** give the richer sensor a distinct name
    like **"Alex Location"**. (See §4.)
- Add **aliases** for the words you'd actually say: a "Living Room TV" media player might get
  aliases "TV," "the telly," "lounge TV." Cooper matches on those too.

**Don't:**
- Don't bury entity IDs, integration prefixes, or units in friendly names ("sensor.0x847f_temp_2").
- Don't give two exposed entities the same name.

---

## 3. Areas & floors — give Cooper a map of the house

Assign **every exposed entity to an Area**, and group Areas under **Floors**.

This is what lets Cooper reason spatially:
- *"Turn off the lights upstairs"* → needs Floors.
- *"Is anything still on in the office?"* → needs Areas.
- *"Is everything okay downstairs?"* → needs both.

**Do:**
- Put devices in the room they're in. Assign cameras, locks, and sensors to areas too, not just
  lights — *"is the garage door open?"* works far better when the garage door is *in* the Garage.
- Create Floors (Upstairs / Downstairs / Basement) and assign areas to them.

**Don't:**
- Don't leave exposed entities area-less. An unplaced entity is invisible to every spatial query.

---

## 4. People, presence & location

Cooper can tell you **who's home and where they actually are** — but only with the right entities
exposed.

**Presence (home/away + which zone):**
- Create a **Person** for each family member and attach their **device tracker(s)** (the Home
  Assistant companion app is the easiest GPS source).
- Define **Zones** for the places that matter — Home, Work, School, Gym. Name them clearly. With
  zones, a tracker reads *"at School"* instead of raw coordinates, and Cooper reports it that way.

**Actual place (town / street / "since when"):**
- Home/away alone is coarse. For a readable address, add a **reverse-geocode** for each person
  (the HACS **Places** integration is the common choice). It turns GPS into a sensor whose *state*
  is a human place, often with a *"(since …)"* last-seen time.
- **Name each one `<Name> Location`** (e.g. "Alex Location") and **expose it.** Cooper is told to
  always prefer these sensors over bare home/away — including for *"where is everyone?"* — so the
  naming and exposure here directly control answer quality.

**Forcing a fresh fix:**
- Cooper's **`refresh_location`** tool requests a current GPS fix and then **notifies the asker
  when the new location lands** (or times out) — it returns immediately and follows up, so a voice
  turn never blocks waiting on a phone. It works through the companion app's location-update
  request (and any per-device "request location" button you wire in).
- For this to work, the person's location sensor must exist and be exposed (above).

---

## 5. Cameras & vision

Cooper can **look at a camera and describe what it sees**, live or (with the right setup) from
recorded footage.

**Live vision (works broadly):**
- Expose your cameras with clear, location-based names: *"Driveway," "Side Yard," "Front Door."*
  Then *"is anyone in the side yard?"* just works.
- Put cameras in their area so spatial queries find them.

**Recorded footage (`look_at_recorded_footage`):**
- Requires Home Assistant's **`media_source`** and a camera/NVR that exposes recordings through it
  (e.g. **Reolink** with local recording). Cooper finds the clip covering a past time and pulls a
  frame to describe.
- This path depends on your NVR's recording being available and authorized to Home Assistant; not
  every camera integration supports it. If recorded-footage lookups fail, live vision still works.

**Don't:**
- Don't expect footage history without a recording backend — there's nothing to look at.

---

## 6. Locks, garages, covers & safety tiering

Cooper's safety tiers are **mechanical and key off the entity's domain + `device_class`** — they
are not the model's discretion. Setting these correctly is what makes "confirm before the risky
stuff" actually fire.

**Do:**
- Set the right **`device_class`** on covers: a garage door should be `device_class: garage` (or
  `gate` / `door`). Those become **confirm-tier** — Cooper asks a yes/no before opening. A plain
  blind/shade stays low-friction.
- Locks are confirm-tier to **unlock** by design. Expose only the locks you want Cooper to touch.
- Start in **Observe mode** (on by default): Cooper says what it *would* do without doing it, so
  you can build trust. Flip it off when ready. The **kill switch** hard-stops all actions.

**Know the limits:**
- Alarm panels have no standard Assist intent, so Cooper won't arm/disarm them by voice — expose
  and automate those deliberately if you want them in scope.
- A few domains are denylisted outright (e.g. `update`, `backup`) and always refused.

---

## 7. History & the recorder

Questions like *"when did the front door last open?"* or *"has the garage been opened today?"*
come from the **recorder**. Keep it enabled, and make sure the entities you ask about are
**recorded** (not excluded in your recorder config). No recorder history → Cooper can only see the
current state.

---

## 8. The Assist pipeline (voice)

- Point an **Assist pipeline** at `conversation.cooper` (your other assistants stay untouched).
- Pick a **low-latency TTS** — Cooper streams its first sentence while it finishes the rest, so a
  fast voice engine is what you actually hear as "snappy."
- Expect simple, literal commands (*"turn on the kitchen light"*) to be answered by Home
  Assistant's **local intent matching before they ever reach Cooper** — that's by design and keeps
  the common case instant. Conversational or ambiguous queries fall through to Cooper.

---

## 9. Memory & personality

- Let Cooper **remember preferences** — tell it lasting things ("I like the bedroom dim at night")
  and it recalls them across conversations.
- Tune **Humor** and **Honesty** (0–100%) in the agent's options to taste; the persona stays
  constant, the dials scale delivery.

---

## Quick verification checklist

After setup (or after any big HA change), sanity-check:

- [ ] The entities I care about are **exposed** to the conversation assistant — and still are after
      recent integration reloads/renames.
- [ ] Every exposed entity has a **clear, unique friendly name**; synonyms are **aliases**.
- [ ] No two exposed entities (especially a **person vs. a location sensor**) share a name.
- [ ] Every exposed entity is in an **Area**, and areas are grouped into **Floors**.
- [ ] Each person has a **Person + device tracker**; meaningful places are **Zones**.
- [ ] Each person has an exposed **`<Name> Location`** geocode sensor (for real addresses).
- [ ] Cameras are exposed with **location names**; if you want footage history, a **media_source**
      NVR is recording.
- [ ] Garage/gate covers have the right **`device_class`**; risky entities are exposed
      deliberately.
- [ ] **Observe mode** is on until you trust it; you know where the **kill switch** is.
- [ ] The **recorder** is enabled for the entities you'll ask history about.

Tick these and Cooper has everything it needs to reason over your home like it's lived there for
years.
