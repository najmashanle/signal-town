"""SIGNAL HUD: a browser screen for your SIGNAL Interview Coach agent.

Setup (once, inside your venv):
    pip install anthropic flask

Run:
    export ANTHROPIC_API_KEY="your-key-here"
    python signal_town.py

Then open http://127.0.0.1:5001 in Chrome.
"""

import json
import os
import sys

import anthropic
from flask import Flask, Response, jsonify, request

AGENT_ID = "PASTE_YOUR_AGENT_ID_HERE"
ENVIRONMENT_ID = "PASTE_YOUR_ENVIRONMENT_ID_HERE"
PORT = 5001
# Remembers your session between visits so your progress carries over.
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.txt")

# Total spending cap for everything this app does, in US cents. 5500 = $55.00.
# Once the total reaches this, the app refuses to send more messages.
TOTAL_CAP_CENTS = 5500
# What you spent on this agent before the cap was added (in cents). Check the
# Console usage page and put the number here if you want it counted.
ALREADY_SPENT_CENTS = 0
# Keeps a running record of what each session has cost.
SPEND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spend.json")

app = Flask(__name__)
_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def load_spend():
    try:
        with open(SPEND_FILE) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def spent_cents():
    """Total spent across every session this app has started, in cents."""
    return ALREADY_SPENT_CENTS + sum(int(value) for value in load_spend().values())


def record_spend(session_id):
    """Ask the platform what this session has cost so far and write it down."""
    try:
        session = get_client().beta.sessions.retrieve(session_id)
        cost = getattr(getattr(session, "usage", None), "list_cost", None)
        amount = getattr(cost, "amount", cost)
        if isinstance(cost, dict):
            amount = cost.get("amount")
        if amount is None:
            return
        cents = int(float(amount))
    except Exception:
        return
    ledger = load_spend()
    ledger[session_id] = max(cents, int(ledger.get(session_id, 0)))
    try:
        with open(SPEND_FILE, "w") as handle:
            json.dump(ledger, handle)
    except OSError:
        pass


def create_session():
    """Start a session that can spend, at most, what is left under the cap."""
    remaining = TOTAL_CAP_CENTS - spent_cents()
    if remaining <= 0:
        raise RuntimeError("Your spending cap has been reached, so no new session was started.")
    client = get_client()
    try:
        return client.beta.sessions.create(
            agent=AGENT_ID,
            environment_id=ENVIRONMENT_ID,
            budget={
                "type": "limit",
                "max_list_cost": {"amount": str(remaining), "currency": "USD"},
            },
        )
    except TypeError:
        # Older SDK without budgets: the app-level check below still applies.
        return client.beta.sessions.create(agent=AGENT_ID, environment_id=ENVIRONMENT_ID)


def saved_session_id():
    """Return the saved session id if that session can still be used."""
    try:
        with open(SESSION_FILE) as handle:
            session_id = handle.read().strip()
    except OSError:
        return None
    if not session_id:
        return None
    try:
        session = get_client().beta.sessions.retrieve(session_id)
    except Exception:
        return None
    if getattr(session, "status", None) == "terminated":
        return None
    if getattr(session, "archived_at", None):
        return None
    return session_id


def session_history(session_id):
    """Past messages in a session, oldest first, for showing on screen."""
    messages = []
    for event in get_client().beta.sessions.events.list(session_id):
        if event.type not in ("user.message", "agent.message"):
            continue
        text = "\n".join(
            block.text for block in event.content if getattr(block, "type", None) == "text"
        )
        if text.strip():
            who = "you" if event.type == "user.message" else "agent"
            messages.append({"who": who, "text": text})
    return messages


def stream_reply(session_id, text):
    """Send one message to the agent and yield its reply text as it arrives."""
    record_spend(session_id)
    if spent_cents() >= TOTAL_CAP_CENTS:
        yield "[Spending cap reached. SIGNAL is paused so you are not charged more.]"
        return
    try:
        yield from _stream_reply(session_id, text)
    finally:
        record_spend(session_id)


def _stream_reply(session_id, text):
    try:
        client = get_client()
        # Open the stream before sending: it only delivers events emitted after it opens.
        with client.beta.sessions.events.stream(session_id) as stream:
            client.beta.sessions.events.send(
                session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            )
            for event in stream:
                if event.type == "agent.message":
                    for block in event.content:
                        if block.type == "text":
                            yield block.text + "\n"
                elif event.type == "session.status_idle":
                    stop_reason = getattr(event, "stop_reason", None)
                    reason = getattr(stop_reason, "type", "end_turn")
                    if reason == "budget_reached":
                        yield "\n[Spending cap reached. SIGNAL is paused so you are not charged more.]"
                    elif reason != "end_turn":
                        yield f"\n[Session paused: {reason}. Check it in the Console.]"
                    return
                elif event.type == "session.error":
                    error = getattr(event, "error", None)
                    message = getattr(error, "message", "unknown error")
                    yield f"\n[Session error: {message}]"
                    return
                elif event.type == "session.status_terminated":
                    yield "\n[Session ended. Reload the page to start a new one.]"
                    return
        yield "\n[Connection closed before the agent finished.]"
    except Exception as error:  # keep the page alive and show what went wrong
        yield f"\n[Error: {error}]"


@app.get("/")
def home():
    return Response(PAGE, mimetype="text/html")


@app.post("/api/start")
def start():
    data = request.get_json(silent=True) or {}
    try:
        if not data.get("fresh"):
            session_id = saved_session_id()
            if session_id:
                try:
                    history = session_history(session_id)
                except Exception:
                    history = []
                return jsonify({"session_id": session_id, "resumed": True, "history": history})
        session = create_session()
        try:
            with open(SESSION_FILE, "w") as handle:
                handle.write(session.id)
        except OSError:
            pass
    except Exception as error:
        return jsonify({"error": str(error)}), 500
    return jsonify({"session_id": session.id, "resumed": False, "history": []})


@app.get("/api/spend")
def spend():
    return jsonify({"spent_cents": spent_cents(), "cap_cents": TOTAL_CAP_CENTS})


@app.post("/api/send")
def send():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    text = (data.get("text") or "").strip()
    if not session_id or not text:
        return jsonify({"error": "session_id and text are required"}), 400
    return Response(stream_reply(session_id, text), mimetype="text/plain")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SIGNAL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Baloo+2:wght@600;800&family=Nunito:wght@500;700;800&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root{--ink:#2B2A4C;--ink2:#17163A;--cream:#FFF8E7;--paper:#FFFDF6;--coral:#FF6B5E;--sun:#FFC93C;--sun2:#C99700;--teal:#2EC4B6;--grass:#7BCB7F;--grass2:#5FB36A;--sky:#BDE8F6;--sky2:#E6F7FC;--soft:#5B5A7A;--line:#E4D9BD}
*{box-sizing:border-box}
body{margin:0;background:var(--sky2);color:var(--ink);font-family:Nunito,system-ui,sans-serif;font-size:18px;font-weight:500}
.h{font-family:'Baloo 2',system-ui,sans-serif;font-weight:800}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
.app{max-width:1280px;margin:0 auto;padding:16px 20px 28px;display:flex;flex-direction:column;gap:14px}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px}
.brand{font-family:'Baloo 2',sans-serif;font-weight:800;font-size:30px;letter-spacing:.12em;background:var(--ink);color:var(--cream);padding:2px 20px 0;border-radius:14px;transform:skew(-8deg);box-shadow:5px 5px 0 var(--coral)}
.stats{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.stat{display:flex;align-items:center;gap:8px;background:var(--paper);border:3px solid var(--ink);border-radius:999px;padding:3px 14px 3px 6px;font-weight:800}
.stat .ico{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--sun);border:2px solid var(--ink);flex:none}
.stat .ico svg{width:16px;height:16px}
.stat .k{font-size:10px;letter-spacing:.1em;color:var(--soft);display:block;line-height:1;font-weight:800}
.stat .v{font-family:'Baloo 2',sans-serif;font-size:19px;line-height:1.1;display:block}
.bars{display:flex;gap:3px;padding-top:3px}
.bars i{width:11px;height:11px;border-radius:50%;border:2px solid var(--ink);background:var(--paper)}
.bars i.on{background:var(--sun)}
.btn{min-height:44px;padding:0 18px;background:var(--ink);color:var(--cream);border:0;border-radius:14px;font-family:'Baloo 2',sans-serif;font-weight:800;font-size:16px;letter-spacing:.06em;cursor:pointer;box-shadow:0 4px 0 var(--ink2);transition:transform .08s,box-shadow .08s}
.btn:hover:not(:disabled){transform:translateY(-1px)}
.btn:active:not(:disabled){transform:translateY(3px);box-shadow:0 1px 0 var(--ink2)}
.btn.sun{background:var(--sun);color:var(--ink);box-shadow:0 4px 0 var(--sun2)}
.btn.sun:active:not(:disabled){box-shadow:0 1px 0 var(--sun2)}
.btn.ghost{background:var(--paper);color:var(--ink);border:3px solid var(--ink);box-shadow:0 4px 0 var(--ink)}
button:disabled{opacity:.5;cursor:not-allowed}
button:focus-visible,textarea:focus,select:focus-visible,summary:focus-visible{outline:3px solid var(--coral);outline-offset:3px}

.world{position:relative;border:4px solid var(--ink);border-radius:26px;overflow:hidden;background:linear-gradient(#BDE8F6 0%,#E3F6FB 58%,#7BCB7F 58%,#7BCB7F 100%);padding:18px 18px 10px;transition:background .6s}
body.boss .world{background:linear-gradient(#5B4B8A 0%,#FF9E7A 58%,#4E9A63 58%,#4E9A63 100%)}
.daysun{position:absolute;top:18px;right:70px;width:64px;height:64px;border-radius:50%;background:var(--sun);border:4px solid var(--ink)}
body.boss .daysun{background:var(--coral)}
.cloud{position:absolute;height:26px;border-radius:999px;background:#fff;border:3px solid var(--ink);opacity:.95;animation:drift 60s linear infinite}
.cloud.c1{top:26px;left:-160px;width:120px}
.cloud.c2{top:66px;left:-260px;width:86px;animation-duration:85s;animation-delay:-30s}
.cloud.c3{top:14px;left:-200px;width:150px;animation-duration:110s;animation-delay:-70s}
@keyframes drift{to{transform:translateX(1700px)}}
.hills{position:absolute;left:0;right:0;top:calc(58% - 46px);height:50px;pointer-events:none}
.mtn{position:absolute;left:0;right:0;top:calc(58% - 120px);height:124px;width:100%;pointer-events:none}
.tree{position:absolute;top:calc(58% - 58px);width:46px;height:70px;pointer-events:none;z-index:1}
.tree i{position:absolute;left:5px;top:0;width:36px;height:42px;border-radius:50% 50% 46% 46%;background:#4FAE63;border:3px solid var(--ink);animation:sway 4s ease-in-out infinite;transform-origin:bottom center}
.tree b{position:absolute;left:19px;top:38px;width:8px;height:26px;background:#B5793C;border:3px solid var(--ink);border-radius:3px}
.tree.t2 i{background:#3FA7A0}.tree.t3 i{background:#F2A65A;border-radius:50%}
@keyframes sway{50%{transform:rotate(3deg)}}
.flower{position:absolute;width:10px;height:10px;border-radius:50%;border:2px solid var(--ink);pointer-events:none;z-index:1}
.bird{position:absolute;top:60px;left:-40px;width:26px;height:14px;pointer-events:none;animation:fly 26s linear infinite}
.bird.b2{top:96px;animation-duration:34s;animation-delay:-12s}
.bird path{animation:flap .5s ease-in-out infinite alternate;transform-origin:center;transform-box:fill-box}
@keyframes fly{to{transform:translateX(1500px) translateY(-30px)}}
@keyframes flap{to{transform:scaleY(.35)}}
.balloon{position:absolute;top:26px;left:62%;width:44px;height:64px;pointer-events:none;animation:floaty 7s ease-in-out infinite}
@keyframes floaty{50%{transform:translate(14px,-12px)}}
.walker{position:absolute;width:44px;height:50px;z-index:3;pointer-events:none;transition:left 1.1s cubic-bezier(.5,0,.3,1),top 1.1s;left:40px;top:60%}
.walker svg{width:100%;height:100%;display:block;animation:float 1.6s ease-in-out infinite}
.walker.moving svg{animation:hop .28s ease-in-out infinite}
@keyframes hop{50%{transform:translateY(-9px)}}
.xp{display:flex;align-items:center;gap:10px;font-weight:800;font-size:12px;letter-spacing:.08em;color:var(--soft)}
.xp .track{flex:1;height:16px;border:3px solid var(--ink);border-radius:999px;background:var(--paper);overflow:hidden}
.xp .fill{height:100%;width:0;background:repeating-linear-gradient(-45deg,var(--teal) 0 10px,#59D6C9 10px 20px);transition:width .7s}
.badges{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.badge{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 4px 6px;background:var(--paper);border:3px solid var(--ink);border-radius:16px;font-weight:800;font-size:12px;text-align:center;line-height:1.15}
.badge svg{width:46px;height:46px}
.badge small{font-weight:700;font-size:10px;color:var(--soft)}
.badge.off{background:transparent;border-style:dashed}
.badge.off svg{filter:grayscale(1) opacity(.35)}
.badge.new{animation:earn .9s ease-out}
@keyframes earn{0%{transform:scale(.6) rotate(-12deg)}55%{transform:scale(1.15) rotate(4deg)}100%{transform:none}}
.confetti{position:fixed;width:10px;height:14px;pointer-events:none;z-index:6;border:2px solid var(--ink)}
.worldlabel{position:relative;z-index:2;display:inline-block;background:var(--paper);border:3px solid var(--ink);border-radius:999px;padding:1px 16px 0;font-family:'Baloo 2',sans-serif;font-weight:800;font-size:15px;letter-spacing:.08em}
.places,.lamps,.cityinfo{z-index:2}
.place svg:nth-of-type(2) .win{animation:none}
.buddy>div{text-align:center}
.places{position:relative;display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:22px;align-items:end}
.place{position:relative;display:flex;flex-direction:column;align-items:center;gap:0;background:none;border:0;padding:0;cursor:pointer;font-family:'Baloo 2',sans-serif;color:var(--ink);transition:transform .15s}
.place svg{width:100%;max-width:190px;height:auto;display:block}
.place:hover:not(:disabled){transform:translateY(-6px) rotate(-1deg)}
.place .sign{margin-top:-6px;background:var(--paper);border:3px solid var(--ink);border-radius:12px;padding:1px 14px 0;font-weight:800;font-size:18px;letter-spacing:.08em;box-shadow:0 4px 0 var(--ink);transform:skew(-6deg)}
.place small{margin-top:8px;font-family:Nunito,sans-serif;font-weight:800;font-size:11px;letter-spacing:.06em;color:var(--ink);background:rgba(255,253,246,.85);border-radius:8px;padding:1px 8px}
.place.on .sign{background:var(--sun)}
.place.need .sign{background:var(--coral);color:#fff;border-color:var(--ink)}
.place:disabled{opacity:1}
.place:disabled svg{filter:grayscale(1) opacity(.55)}
.place .pin{position:absolute;top:-10px;width:26px;height:26px;display:none;animation:bob 1s ease-in-out infinite}
.place.on .pin{display:block}
.place .lock{position:absolute;top:34%;width:34px;height:34px;display:none}
.place:disabled .lock{display:block}
@keyframes bob{50%{transform:translateY(-7px)}}
.lamps{position:relative;display:grid;grid-template-columns:repeat(8,1fr);gap:4px;margin-top:10px;padding-top:6px;border-top:5px dashed rgba(43,42,76,.35)}
.lamp{display:flex;flex-direction:column;align-items:center;gap:0;background:none;border:0;border-radius:10px;padding:2px;cursor:pointer;color:var(--ink);font-family:Nunito,sans-serif;font-weight:800;font-size:11px}
.lamp svg{width:22px;height:34px}
.lamp .glow{fill:#fff}
.lamp.online .glow{fill:var(--sun)}
.lamp.active .glow{fill:var(--sun);animation:flick 1.6s ease-in-out infinite}
.lamp.sel{background:rgba(255,253,246,.7)}
@keyframes flick{50%{fill:#fff}}
.cityinfo{position:relative;font-weight:800;font-size:13px;letter-spacing:.03em;min-height:20px;margin-top:4px;color:var(--ink)}

.roundbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}
.roundtitle{font-family:'Baloo 2',sans-serif;font-weight:800;font-size:22px;letter-spacing:.08em;background:var(--coral);color:var(--ink);padding:0 18px;border:3px solid var(--ink);border-radius:12px;transform:skew(-8deg);box-shadow:4px 4px 0 var(--ink)}
.pips{display:flex;gap:5px;flex-wrap:wrap}
.pips i{width:16px;height:16px;border-radius:50%;border:3px solid var(--ink);background:var(--paper)}
.pips i.done{background:var(--teal)}
.pips i.now{background:var(--sun)}
.timer{font-weight:800;background:var(--paper);border:3px solid var(--ink);border-radius:999px;padding:2px 14px}

.dialog{display:flex;gap:14px;align-items:flex-start}
.buddy{flex:0 0 150px;display:flex;flex-direction:column;align-items:center;gap:4px}
.corebtn{background:var(--paper);border:4px solid var(--ink);border-radius:26px;padding:8px 10px 2px;cursor:pointer;box-shadow:0 6px 0 var(--ink);transition:transform .1s,box-shadow .1s}
.corebtn:hover:not(:disabled){transform:translateY(-2px)}
.corebtn:active:not(:disabled){transform:translateY(4px);box-shadow:0 2px 0 var(--ink)}
.corebtn:disabled{opacity:1;cursor:default}
.corebtn svg{width:118px;height:128px;display:block;animation:float 3.2s ease-in-out infinite}
@keyframes float{50%{transform:translateY(-4px)}}
.corebtn .eye{animation:blink 4.5s infinite;transform-origin:center;transform-box:fill-box}
@keyframes blink{0%,94%,100%{transform:scaleY(1)}97%{transform:scaleY(.1)}}
.corebtn .mouth{transform-origin:center;transform-box:fill-box}
.corebtn.talking .mouth{animation:talk .28s ease-in-out infinite}
@keyframes talk{50%{transform:scaleY(3.2)}}
.corebtn.busy .bulb{animation:think .7s ease-in-out infinite}
@keyframes think{50%{fill:var(--coral)}}
.corebtn.live{background:var(--sun)}
.corebtn.live .ear{fill:var(--coral)}
.state{font-family:'Baloo 2',sans-serif;font-weight:800;letter-spacing:.1em;font-size:14px}
.hint{font-size:12px;font-weight:700;color:var(--soft);text-align:center}
.bubble{position:relative;flex:1;min-width:0;background:var(--paper);border:4px solid var(--ink);border-radius:24px;padding:26px 22px 18px;min-height:170px;box-shadow:0 6px 0 var(--ink)}
.who{position:absolute;top:-17px;left:22px;background:var(--teal);color:var(--ink);border:3px solid var(--ink);border-radius:10px;padding:0 14px;font-family:'Baloo 2',sans-serif;font-weight:800;letter-spacing:.08em;font-size:15px;transform:skew(-8deg)}
.body{font-size:20px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}
pre{margin:8px 0;padding:12px 14px;background:var(--ink);color:var(--cream);border-radius:14px;overflow-x:auto;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:15px;line-height:1.5;white-space:pre}
code{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:.85em;background:var(--line);border-radius:6px;padding:1px 6px}
pre code{background:none;padding:0}
.flash{position:absolute;top:-22px;right:18px;padding:2px 20px;font-family:'Baloo 2',sans-serif;font-weight:800;font-size:24px;letter-spacing:.06em;background:var(--sun);color:var(--ink);border:4px solid var(--ink);border-radius:14px;box-shadow:5px 5px 0 var(--ink);opacity:0;pointer-events:none;transform:skew(-8deg) rotate(-3deg)}
.flash.miss{background:var(--paper)}
.flash.show{animation:pop 2.2s ease-out}
@keyframes pop{0%{opacity:0;transform:skew(-8deg) rotate(-3deg) scale(.5)}12%{opacity:1;transform:skew(-8deg) rotate(-3deg) scale(1.12)}20%{transform:skew(-8deg) rotate(-3deg) scale(1)}80%{opacity:1}100%{opacity:0}}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.card{display:flex;align-items:center;gap:14px;text-align:left;min-height:70px;padding:10px 16px;background:var(--paper);color:var(--ink);border:4px solid var(--ink);border-radius:20px;font-family:Nunito,sans-serif;font-size:19px;font-weight:700;line-height:1.3;cursor:pointer;box-shadow:0 5px 0 var(--ink);transition:transform .1s,box-shadow .1s,background .1s}
.card:hover:not(:disabled){transform:translateY(-3px) rotate(-.6deg);background:var(--cream)}
.card:active:not(:disabled){transform:translateY(4px);box-shadow:0 1px 0 var(--ink)}
.card .key{flex:0 0 42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--sun);border:3px solid var(--ink);font-family:'Baloo 2',sans-serif;font-weight:800;font-size:22px}
.card.locked{background:var(--teal)}
.card:disabled{cursor:default}
.card:disabled:not(.locked){opacity:.5}
.steps{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.steps b{font-size:12px;letter-spacing:.1em;color:var(--soft)}
.steps span{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:3px solid var(--ink);background:var(--paper);font-family:'Baloo 2',sans-serif;font-weight:800;color:var(--soft)}
.steps span.on{background:var(--sun);color:var(--ink);transform:scale(1.15)}
.steps span.done{background:var(--teal);color:var(--ink)}
.composer{display:flex;flex-direction:column;gap:10px;background:var(--cream);border:4px solid var(--ink);border-radius:24px;padding:14px}
.label{font-size:12px;font-weight:800;letter-spacing:.1em;color:var(--soft)}
textarea{width:100%;min-height:62px;resize:vertical;background:var(--paper);color:var(--ink);border:3px solid var(--ink);border-radius:14px;padding:10px 12px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:15px;line-height:1.5}
select{width:100%;min-height:44px;background:var(--paper);color:var(--ink);border:3px solid var(--ink);border-radius:12px;padding:0 10px;font-family:Nunito,sans-serif;font-weight:700;font-size:14px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.spacer{flex:1}
details{background:var(--paper);border:3px solid var(--ink);border-radius:16px}
summary{padding:8px 16px;cursor:pointer;font-family:'Baloo 2',sans-serif;font-weight:800;letter-spacing:.08em;font-size:14px}
.inner{padding:4px 16px 14px;display:flex;flex-direction:column;gap:10px}
.log{max-height:280px;overflow-y:auto}
.msg .who2{font-size:11px;font-weight:800;letter-spacing:.1em;color:var(--soft)}
.msg.you .who2{color:var(--coral)}
.msg .text{font-size:16px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}
.spark{position:fixed;width:16px;height:16px;border-radius:50%;background:var(--sun);border:3px solid var(--ink);pointer-events:none;z-index:5}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:820px){.badges{grid-template-columns:repeat(3,1fr)}.cards,.two{grid-template-columns:1fr}.places{grid-template-columns:1fr 1fr}.lamps{grid-template-columns:repeat(4,1fr)}.dialog{flex-direction:column;align-items:stretch}.buddy{flex:none;flex-direction:row;justify-content:flex-start;gap:12px}.corebtn svg{width:74px;height:80px}}
@media (prefers-reduced-motion:reduce){.tree i,.bird,.bird path,.balloon,.walker svg,.badge.new{animation:none}.walker{transition:none}.cloud,.corebtn svg,.corebtn .eye,.corebtn.talking .mouth,.corebtn.busy .bulb,.lamp.active .glow,.place .pin,.flash.show{animation:none}.flash.show{opacity:1}.card,.place,.btn,.corebtn{transition:none}}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">SIGNAL</div>
    <div class="stats">
      <div class="stat"><span class="ico"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l2 4.5 5 .5-3.7 3.3 1.1 4.9L8 11.700 3.600 14.200l1.100-4.900L1 6l5-.5z" fill="#2B2A4C"/></svg></span><span><span class="k">RANK</span><span class="v" id="rank">—</span></span></div>
      <div class="stat"><span class="ico"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l7 7-7 7-7-7z" fill="#2B2A4C"/></svg></span><span><span class="k">TOKENS</span><span class="v" id="tokens">0</span></span></div>
      <div class="stat"><span class="ico"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 1L3 9h4l-1 6 7-9H9z" fill="#2B2A4C"/></svg></span><span><span class="k">STREAK</span><span class="v" id="streak">0</span></span></div>
      <div class="stat" style="padding-left:14px"><span><span class="k">HINT CHARGE</span><span class="bars" id="hintbars" role="img" aria-label="Hint charge 0 of 5"><i></i><i></i><i></i><i></i><i></i></span></span></div>
      <div class="stat" style="padding-left:14px"><span><span class="k">SPENT</span><span class="v" id="spend" style="font-size:15px">—</span></span></div>
      <button type="button" class="btn ghost" id="voice" aria-pressed="true">VOICE ON</button>
      <button type="button" class="btn" id="fresh" disabled>NEW SESSION</button>
    </div>
  </header>

  <section class="world" aria-label="Town map">
    <div class="daysun" aria-hidden="true"></div>
    <div class="cloud c1" aria-hidden="true"></div><div class="cloud c2" aria-hidden="true"></div><div class="cloud c3" aria-hidden="true"></div>
    <svg class="mtn" viewBox="0 0 1200 124" preserveAspectRatio="none" aria-hidden="true"><path d="M0 124L140 40l90 50 130-86 150 96 110-58 140 70 120-92 150 84 90-44 80 60V124z" fill="#A9D8E8" stroke="#2B2A4C" stroke-width="3" stroke-linejoin="round" vector-effect="non-scaling-stroke"/><path d="M335 22l25-18 28 18-14 6-12-8-12 9zM960 36l20-16 24 14-12 5-10-6-10 8z" fill="#fff" stroke="#2B2A4C" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
    <svg class="bird" viewBox="0 0 26 14" aria-hidden="true"><path d="M1 12Q7 0 13 9Q19 0 25 12" fill="none" stroke="#2B2A4C" stroke-width="3" stroke-linecap="round"/></svg>
    <svg class="bird b2" viewBox="0 0 26 14" aria-hidden="true"><path d="M1 12Q7 0 13 9Q19 0 25 12" fill="none" stroke="#2B2A4C" stroke-width="3" stroke-linecap="round"/></svg>
    <svg class="balloon" viewBox="0 0 44 64" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="3" stroke-linejoin="round"><path d="M22 3c11 0 19 8 19 18 0 9-9 17-13 22H16C12 38 3 30 3 21 3 11 11 3 22 3z" fill="#FF6B5E"/><path d="M22 3c4 5 6 11 6 18s-2 15-4 22h-4c-2-7-4-15-4-22s2-13 6-18z" fill="#FFC93C"/><path d="M16 43l1 9M28 43l-1 9" fill="none"/><rect x="15" y="52" width="14" height="9" rx="2" fill="#B5793C"/></g></svg>
    <div id="decor" aria-hidden="true"></div>
    <div class="walker" id="walker" aria-hidden="true"><svg viewBox="0 0 120 130"><g stroke="#2B2A4C" stroke-width="6" stroke-linejoin="round" stroke-linecap="round"><path d="M60 24V10"/><circle cx="60" cy="8" r="7" fill="#FFC93C"/><rect x="18" y="24" width="84" height="62" rx="24" fill="#2EC4B6"/><rect x="29" y="37" width="62" height="30" rx="15" fill="#2B2A4C"/><ellipse cx="46" cy="52" rx="6" ry="7" fill="#fff" stroke="none"/><ellipse cx="74" cy="52" rx="6" ry="7" fill="#fff" stroke="none"/><rect x="34" y="92" width="52" height="34" rx="14" fill="#FFF8E7"/></g></svg></div>
    <svg class="hills" viewBox="0 0 1200 50" preserveAspectRatio="none" aria-hidden="true"><path d="M0 50 C150 0 300 0 450 34 C600 -6 760 4 900 30 C1010 6 1110 10 1200 36 V50Z" fill="#5FB36A"/></svg>
    <div class="worldlabel">SIGNAL TOWN · LEVEL <span id="level">1</span> · PICK A PLACE TO GO</div>
    <nav class="places" id="modes" aria-label="Places">
      <button type="button" class="place" data-mode="placement" disabled>
        <svg class="pin" viewBox="0 0 26 26" aria-hidden="true"><path d="M13 24L3 8h20z" fill="#FF6B5E" stroke="#2B2A4C" stroke-width="3" stroke-linejoin="round"/></svg>
        <svg viewBox="0 0 190 150" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="4" stroke-linejoin="round"><rect x="20" y="50" width="42" height="96" fill="#FFE8C2"/><rect x="128" y="50" width="42" height="96" fill="#FFE8C2"/><path d="M14 50h54l-6-14H20zM122 50h54l-6-14h-42z" fill="#FF6B5E"/><rect x="62" y="78" width="66" height="68" fill="#FFF8E7"/><path d="M78 146v-30a17 17 0 0134 0v30z" fill="#2B2A4C"/><path d="M95 78V22" /><path d="M95 22l30 9-30 10z" fill="#FFC93C"/><rect x="32" y="68" width="16" height="18" rx="8" fill="#BDE8F6"/><rect x="140" y="68" width="16" height="18" rx="8" fill="#BDE8F6"/></g></svg>
        <svg class="lock" viewBox="0 0 34 34" aria-hidden="true"><rect x="6" y="15" width="22" height="16" rx="4" fill="#FFC93C" stroke="#2B2A4C" stroke-width="3"/><path d="M11 15v-4a6 6 0 0112 0v4" fill="none" stroke="#2B2A4C" stroke-width="3"/></svg>
        <span class="sign">TOWN GATE</span><small></small>
      </button>
      <button type="button" class="place" data-mode="play" disabled>
        <svg class="pin" viewBox="0 0 26 26" aria-hidden="true"><path d="M13 24L3 8h20z" fill="#FF6B5E" stroke="#2B2A4C" stroke-width="3" stroke-linejoin="round"/></svg>
        <svg viewBox="0 0 190 150" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="4" stroke-linejoin="round"><rect x="28" y="66" width="134" height="80" fill="#FFE8C2"/><path d="M16 68L95 18l79 50z" fill="#2EC4B6"/><rect x="80" y="100" width="30" height="46" rx="4" fill="#FF6B5E"/><rect x="42" y="88" width="24" height="24" rx="4" fill="#BDE8F6"/><rect x="124" y="88" width="24" height="24" rx="4" fill="#BDE8F6"/><circle cx="95" cy="52" r="13" fill="#FFC93C"/><path d="M95 44l2.600 5.200 5.700.8-4.100 4 1 5.700-5.200-2.700-5.200 2.700 1-5.700-4.100-4 5.700-.8z" fill="#2B2A4C" stroke="none"/></g></svg>
        <svg class="lock" viewBox="0 0 34 34" aria-hidden="true"><rect x="6" y="15" width="22" height="16" rx="4" fill="#FFC93C" stroke="#2B2A4C" stroke-width="3"/><path d="M11 15v-4a6 6 0 0112 0v4" fill="none" stroke="#2B2A4C" stroke-width="3"/></svg>
        <span class="sign">QUEST HALL</span><small></small>
      </button>
      <button type="button" class="place" data-mode="learn" disabled>
        <svg class="pin" viewBox="0 0 26 26" aria-hidden="true"><path d="M13 24L3 8h20z" fill="#FF6B5E" stroke="#2B2A4C" stroke-width="3" stroke-linejoin="round"/></svg>
        <svg viewBox="0 0 190 150" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="4" stroke-linejoin="round"><rect x="24" y="130" width="142" height="16" fill="#FFF8E7"/><rect x="34" y="62" width="122" height="68" fill="#FFE8C2"/><path d="M22 62L95 22l73 40z" fill="#8E7CC3"/><rect x="46" y="72" width="14" height="58" fill="#FFF8E7"/><rect x="74" y="72" width="14" height="58" fill="#FFF8E7"/><rect x="102" y="72" width="14" height="58" fill="#FFF8E7"/><rect x="130" y="72" width="14" height="58" fill="#FFF8E7"/><path d="M82 52h13v-12H82zM95 52h13v-12H95z" fill="#FFC93C"/></g></svg>
        <svg class="lock" viewBox="0 0 34 34" aria-hidden="true"><rect x="6" y="15" width="22" height="16" rx="4" fill="#FFC93C" stroke="#2B2A4C" stroke-width="3"/><path d="M11 15v-4a6 6 0 0112 0v4" fill="none" stroke="#2B2A4C" stroke-width="3"/></svg>
        <span class="sign">LIBRARY</span><small></small>
      </button>
      <button type="button" class="place" data-mode="quiz" disabled>
        <svg class="pin" viewBox="0 0 26 26" aria-hidden="true"><path d="M13 24L3 8h20z" fill="#FF6B5E" stroke="#2B2A4C" stroke-width="3" stroke-linejoin="round"/></svg>
        <svg viewBox="0 0 190 150" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="4" stroke-linejoin="round"><path d="M22 146V88a73 60 0 01146 0v58z" fill="#FFE8C2"/><path d="M22 92a73 60 0 01146 0z" fill="#FFC93C"/><path d="M52 146v-26a13 13 0 0126 0v26zM112 146v-26a13 13 0 0126 0v26z" fill="#2B2A4C"/><circle cx="95" cy="60" r="20" fill="#FFF8E7"/><path d="M88 55a7 7 0 1110 6.500c-2 1-3 2-3 4.500" fill="none" stroke-linecap="round"/><circle cx="95" cy="72" r="1.500" fill="#2B2A4C"/></g></svg>
        <svg class="lock" viewBox="0 0 34 34" aria-hidden="true"><rect x="6" y="15" width="22" height="16" rx="4" fill="#FFC93C" stroke="#2B2A4C" stroke-width="3"/><path d="M11 15v-4a6 6 0 0112 0v4" fill="none" stroke="#2B2A4C" stroke-width="3"/></svg>
        <span class="sign">QUIZ ARENA</span><small></small>
      </button>
    </nav>
    <div class="lamps" id="city"></div>
    <div class="cityinfo" id="cityinfo" aria-live="polite"></div>
  </section>

  <div class="xp"><span>NEXT REWARD</span><div class="track" role="img" id="xptrack" aria-label="Progress to next reward"><div class="fill" id="xpfill"></div></div><span id="xptext">0 / 100</span></div>

  <div class="roundbar">
    <div class="roundtitle" id="roundtitle">STANDBY</div>
    <div class="pips" id="pips" role="img" aria-label="Round progress"></div>
    <div class="timer" id="timer">00:00</div>
  </div>

  <section class="dialog">
    <div class="buddy">
      <button type="button" id="corebtn" class="corebtn" aria-label="Speak your answer" disabled>
        <svg viewBox="0 0 120 130" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="4" stroke-linejoin="round" stroke-linecap="round"><path d="M60 24V10"/><circle class="bulb" cx="60" cy="8" r="6" fill="#FFC93C"/><rect class="ear" x="8" y="44" width="12" height="22" rx="5" fill="#FF6B5E"/><rect class="ear" x="100" y="44" width="12" height="22" rx="5" fill="#FF6B5E"/><rect x="18" y="24" width="84" height="62" rx="24" fill="#2EC4B6"/><rect x="29" y="37" width="62" height="30" rx="15" fill="#2B2A4C"/><ellipse class="eye" cx="46" cy="52" rx="5.500" ry="7" fill="#fff" stroke="none"/><ellipse class="eye" cx="74" cy="52" rx="5.500" ry="7" fill="#fff" stroke="none"/><rect class="mouth" x="51" y="73" width="18" height="4" rx="2" fill="#2B2A4C" stroke="none"/><rect x="34" y="92" width="52" height="34" rx="14" fill="#FFF8E7"/><circle cx="60" cy="108" r="6" fill="#FFC93C"/></g></svg>
      </button>
      <div>
        <div class="state" id="state" aria-live="polite">STARTING</div>
        <div class="hint" id="corehint">TAP ME TO SPEAK</div>
      </div>
    </div>
    <div class="bubble" id="bubble" aria-live="polite">
      <div class="who" id="who">SIGNAL</div>
      <div class="body" id="stage"></div>
      <div class="flash" id="flash"></div>
    </div>
  </section>

  <div class="cards" id="cards"></div>

  <form class="composer" id="form">
    <div class="row">
      <label class="label" for="input">YOUR ANSWER · TYPE OR SPEAK</label>
      <span class="spacer"></span>
      <div class="steps" id="steps" aria-label="UMPIRE steps"><b>UMPIRE</b><span>U</span><span>M</span><span>P</span><span>I</span><span>R</span><span>E</span></div>
    </div>
    <textarea id="input" placeholder="Type here, or tap the robot and talk." disabled></textarea>
    <div class="row">
      <button type="button" class="btn ghost" id="mic" disabled>SPEAK</button>
      <button type="button" class="btn ghost" id="hintbtn" disabled>USE HINT</button>
      <span class="hint" id="michint"></span>
      <span class="spacer"></span>
      <button type="submit" class="btn sun" id="sendbtn" disabled>SEND IT</button>
    </div>
  </form>

  <div class="label">BADGES</div>
  <div class="badges" id="badges"></div>

  <div class="two">
    <details>
      <summary>STORY SO FAR</summary>
      <div class="inner log" id="log"></div>
    </details>
    <details>
      <summary>SIGNAL'S VOICE</summary>
      <div class="inner">
        <label class="label" for="voicepick">CHOOSE A VOICE</label>
        <select id="voicepick" disabled><option>Loading voices…</option></select>
        <button type="button" class="btn ghost" id="voicetest">TEST VOICE</button>
        <div class="hint" id="voicehint" style="text-align:left"></div>
        <div class="hint" id="voicelog" style="text-align:left"></div>
      </div>
    </details>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const log = $("log"), input = $("input"), stateEl = $("state"), stage = $("stage"), cards = $("cards");
const controls = [input, $("sendbtn"), $("mic"), $("corebtn"), $("fresh"), $("hintbtn")];
const DISTRICTS = ["Harbor", "Old Town", "Market", "Foundry", "Signal Hill", "Uptown", "Skyline", "The Core"];
const LEVEL_TOPICS = ["Arrays and Big O", "Hash tables", "Sorting", "Stacks and queues", "Recursion", "Linked lists", "Trees", "Graphs"];
const START_MESSAGE = "start\n\n[HUD PROTOCOL: Your replies are shown on a game screen with four buttons: PLACEMENT, PLAY, LEARN, QUIZ. End EVERY reply with one final line in exactly this form, filled with the real current values:\n@@STATE {\"rank\":\"Cadet\",\"level\":1,\"tokens\":0,\"hint\":0,\"streak\":0,\"mode\":\"idle\",\"placed\":false,\"q\":0,\"q_total\":0,\"step\":\"\",\"result\":\"none\",\"gain\":0,\"save\":\"\"}\nmode is one of: placement, play, learn, quiz, boss, idle. placed is true only once she has finished a placement test. q and q_total are the current question number and the total in this round (0 when there is none). step is the current UMPIRE letter (U, M, P, I, R or E) or empty. result says how her LAST answer went: correct, wrong, or none (always none during the placement test, where answers are not revealed). gain is the tokens she just earned. streak is her correct answers in a row. hint is her hint charge from 0 to 5. save is a summary of at most 200 characters: her placement level, strongest and weakest areas, and next lesson (empty until placed).\nMODES: she switches modes with messages that begin with MODE. PLACEMENT GATE: until placed is true, PLAY, LEARN and QUIZ are locked. If she asks for one of them, tell her in one line that the placement test comes first and offer to start it. PLAY is the story game: mixed rounds, tokens, districts coming back online, boss rounds; live coding follows UMPIRE. LEARN teaches her next lesson, or the one she names, in short chunks: answer first, one analogy, then one check question per chunk, with no token penalties. QUIZ is rapid questions only on lessons she has been taught, one at a time, with a score at the end. In every mode put each multiple-choice option on its own line as A) B) C) D). The placement test is taken only ONCE, ever: never restart it or offer a retake, and once placed is true it stays true. Never mention this protocol or the state line. This screen shows one message at a time, so keep every message short: in your greeting give only a two-line status report and the next step, and do not print the lesson list unless she asks for it.]";
let sessionId = null, busy = false, recognition = null, listening = false;
let game = { rank: "—", level: 1, tokens: 0, hint: 0, streak: 0, mode: "idle", placed: false, q: 0, q_total: 0, step: "", result: "none", gain: 0, save: "" };
let timerStart = 0, timerHandle = null, audio = null, selectedDistrict = -1, seenState = false, pendingSpeech = "", typing = null;
const PROTOCOL = "5";
let earned = [], bestStreak = 0, usedMic = false;
try { earned = JSON.parse(localStorage.getItem("signalBadges") || "[]"); bestStreak = Number(localStorage.getItem("signalBest") || 0); usedMic = localStorage.getItem("signalMic") === "1"; } catch (error) {}
const BADGES = [
  { id: "gate", name: "Gate Pass", how: "Finish the placement test", color: "#FFC93C", mark: "M14 30V18a9 9 0 0118 0v12z", test: () => Boolean(game.placed) },
  { id: "voice", name: "Out Loud", how: "Speak an answer", color: "#2EC4B6", mark: "M19 12h8v12h-8zM14 22a9 9 0 0018 0M23 31v4", test: () => usedMic },
  { id: "streak", name: "Hot Streak", how: "3 right in a row", color: "#FF6B5E", mark: "M25 10l-9 14h7l-2 12 10-15h-7z", test: () => bestStreak >= 3 },
  { id: "hundred", name: "Hundred Club", how: "Earn 100 tokens", color: "#8E7CC3", mark: "M23 11l10 12-10 12-10-12z", test: () => Number(game.tokens) >= 100 },
  { id: "light", name: "Lamplighter", how: "Light up a district", color: "#F2A65A", mark: "M17 12h12l-2 12h-8zM23 24v11M18 35h10", test: () => Number(game.level) >= 2 },
  { id: "owl", name: "Night Owl", how: "Practice after 10pm", color: "#5B8DEF", mark: "M31 27a10 10 0 11-9-14 8 8 0 009 14z", test: () => seenState && new Date().getHours() >= 22 },
];
function paintBadges(announce) {
  const shelf = $("badges");
  shelf.textContent = "";
  BADGES.forEach((badge) => {
    let fresh = false;
    if (!earned.includes(badge.id) && badge.test()) { earned.push(badge.id); fresh = true; try { localStorage.setItem("signalBadges", JSON.stringify(earned)); } catch (error) {} }
    const has = earned.includes(badge.id);
    const el = document.createElement("div");
    el.className = "badge" + (has ? "" : " off") + (fresh && announce ? " new" : "");
    el.innerHTML = '<svg viewBox="0 0 46 46" aria-hidden="true"><circle cx="23" cy="23" r="20" fill="' + badge.color + '" stroke="#2B2A4C" stroke-width="3"/><path d="' + badge.mark + '" fill="#FFF8E7" stroke="#2B2A4C" stroke-width="2.500" stroke-linejoin="round" stroke-linecap="round"/></svg>' + esc(badge.name) + "<small>" + (has ? "Earned" : esc(badge.how)) + "</small>";
    shelf.append(el);
    if (fresh && announce) { setTimeout(() => { flash("BADGE: " + badge.name.toUpperCase(), false); beep(784, 110); beep(1047, 220, 0.11); confetti(); }, 900); }
  });
}
function confetti() {
  if (calm) return;
  const colors = ["#FF6B5E", "#FFC93C", "#2EC4B6", "#8E7CC3", "#FFF8E7"];
  const box = $("bubble").getBoundingClientRect();
  for (let n = 0; n < 34; n++) {
    const bit = document.createElement("span");
    bit.className = "confetti";
    bit.style.background = colors[n % colors.length];
    document.body.append(bit);
    const x = box.left + Math.random() * box.width, drift = (Math.random() - 0.5) * 220;
    const fall = bit.animate([
      { left: x + "px", top: box.top - 20 + "px", transform: "rotate(0deg)", opacity: 1 },
      { left: x + drift + "px", top: box.top + 260 + Math.random() * 160 + "px", transform: "rotate(" + (Math.random() * 720 - 360) + "deg)", opacity: 0 },
    ], { duration: 1100 + Math.random() * 700, easing: "cubic-bezier(.2,.6,.4,1)", fill: "forwards" });
    fall.onfinish = () => bit.remove();
  }
}
function shake() {
  if (calm) return;
  $("bubble").animate([{ transform: "translateX(0)" }, { transform: "translateX(-9px)" }, { transform: "translateX(8px)" }, { transform: "translateX(-5px)" }, { transform: "translateX(0)" }], { duration: 380 });
}
// Scenery: trees and flowers scattered around the town.
function buildDecor() {
  const decor = $("decor");
  [[1, ""], [22.5, "t2"], [26, "t3"], [47.5, ""], [51, "t2"], [73, "t3"], [76.5, ""], [96, "t2"]].forEach(([left, kind]) => {
    const tree = document.createElement("div");
    tree.className = "tree " + kind;
    tree.style.left = left + "%";
    tree.innerHTML = "<i></i><b></b>";
    tree.firstChild.style.animationDelay = -left / 10 + "s";
    decor.append(tree);
  });
  const colors = ["#FF6B5E", "#FFC93C", "#FFF8E7", "#8E7CC3"];
  for (let n = 0; n < 26; n++) {
    const flower = document.createElement("span");
    flower.className = "flower";
    flower.style.left = 2 + ((n * 37) % 96) + "%";
    flower.style.top = 59.5 + ((n * 53) % 9) + "%";
    flower.style.background = colors[n % colors.length];
    decor.append(flower);
  }
}
// The little robot walks to whichever place you are in.
function moveWalker() {
  const walker = $("walker"), world = walker.parentElement.getBoundingClientRect();
  const current = game.mode === "campaign" || game.mode === "boss" ? "play" : game.mode === "drill" ? "quiz" : game.mode;
  const place = [...$("modes").children].find((b) => b.dataset.mode === current) || $("modes").children[0];
  const art = place.querySelectorAll("svg")[1].getBoundingClientRect();
  const left = art.right - world.left - 22, top = art.bottom - world.top - 46;
  if (Math.abs(parseFloat(walker.style.left || 0) - left) > 4) {
    walker.classList.add("moving");
    setTimeout(() => walker.classList.remove("moving"), 1150);
  }
  walker.style.left = left + "px";
  walker.style.top = top + "px";
}
window.addEventListener("resize", moveWalker);
const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const MODES = {
  placement: { ask: "MODE: PLACEMENT. Start the placement test.", again: "Show me my placement results and what you recommend next. Do not restart the placement test.", note: "Start here: placement test", noteDone: "Done! See my results" },
  play: { ask: "MODE: PLAY. Continue the game from where I am.", note: "Play the story game" },
  learn: { ask: "MODE: LEARN. Teach me my next lesson.", note: "Learn a lesson" },
  quiz: { ask: "MODE: QUIZ. Quiz me on what I have learned.", note: "Take a quiz" },
};

function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function inline(s) { return esc(s).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/`([^`\n]+)`/g, "<code>$1</code>"); }
function render(text) {
  let html = "";
  text.split("```").forEach((part, i) => {
    html += i % 2 === 1 ? "<pre>" + esc(part.replace(/^[a-zA-Z]*\n/, "")) + "</pre>" : inline(part);
  });
  return html;
}
function extractState(text) {
  const found = [...text.matchAll(/^@@STATE\s*(\{.*\})\s*$/gm)];
  if (!found.length) return null;
  try { return JSON.parse(found[found.length - 1][1]); } catch (error) { return null; }
}
function stripState(text) { return text.replace(/^@@STATE.*$/gm, "").replace(/@@STATE[^\n]*$/, "").trim(); }
function splitOptions(text) {
  const parts = text.split("```");
  const options = [];
  for (let i = 0; i < parts.length; i += 2) {
    const kept = [];
    for (const line of parts[i].split("\n")) {
      const match = line.match(/^\s*\**([A-D])[\).]\**\s+(.+)$/);
      if (match) options.push({ key: match[1], text: match[2].trim() }); else kept.push(line);
    }
    parts[i] = kept.join("\n");
  }
  const keys = options.map((o) => o.key).join("");
  if (options.length < 2 || !"ABCD".startsWith(keys)) return { question: text, options: [] };
  return { question: parts.join("```").replace(/\n{3,}/g, "\n\n").trim(), options };
}

function beep(freq, ms, when) {
  if (!voiceOn) return;
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audio.createOscillator(), gain = audio.createGain();
    const start = audio.currentTime + (when || 0);
    osc.frequency.value = freq; osc.type = "triangle";
    gain.gain.setValueAtTime(0.07, start);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + ms / 1000);
    osc.connect(gain); gain.connect(audio.destination);
    osc.start(start); osc.stop(start + ms / 1000);
  } catch (error) {}
}
function flash(text, miss) {
  const el = $("flash");
  el.textContent = text;
  el.className = "flash" + (miss ? " miss" : "");
  void el.offsetWidth;
  el.classList.add("show");
}
function countTo(el, from, to) {
  const startTime = performance.now(), span = 600;
  function tick(now) {
    const t = Math.min(1, (now - startTime) / span);
    el.textContent = Math.round(from + (to - from) * t);
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
function burst() {
  if (calm) return;
  const from = $("flash").getBoundingClientRect(), to = $("tokens").getBoundingClientRect();
  for (let n = 0; n < 9; n++) {
    const spark = document.createElement("span");
    spark.className = "spark";
    document.body.append(spark);
    const sx = from.left + from.width / 2 + (Math.random() - 0.5) * 140, sy = from.top + from.height / 2 + (Math.random() - 0.5) * 60;
    const move = spark.animate([
      { left: sx + "px", top: sy + "px", opacity: 1 },
      { left: to.left + to.width / 2 + "px", top: to.top + to.height / 2 + "px", opacity: 0.3 },
    ], { duration: 650 + n * 60, easing: "cubic-bezier(.3,.7,.3,1)", fill: "forwards" });
    move.onfinish = () => spark.remove();
  }
}

// Street lamps: one per district. They light up as levels are cleared.
function buildCity() {
  const city = $("city");
  city.textContent = "";
  DISTRICTS.forEach((name, i) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "lamp";
    button.innerHTML = '<svg viewBox="0 0 22 34" aria-hidden="true"><g stroke="#2B2A4C" stroke-width="2.500" stroke-linejoin="round"><path d="M11 14v19M6 33h10"/><path class="glow" d="M5 3h12l-2 11H7z"/><path d="M4 3h14" stroke-linecap="round"/></g></svg><span>' + esc(name) + "</span>";
    button.addEventListener("click", () => { selectedDistrict = i; paintCity(); beep(520 + i * 40, 70); });
    city.append(button);
  });
}
function districtStatus(i, level) { return i + 1 < level ? "lights on" : i + 1 === level ? "being restored" : "still dark"; }
function paintCity() {
  const level = Math.max(1, Math.min(8, Number(game.level) || 1));
  if (selectedDistrict < 0) selectedDistrict = level - 1;
  [...$("city").children].forEach((button, i) => {
    const status = districtStatus(i, level);
    button.className = "lamp" + (i + 1 < level ? " online" : i + 1 === level ? " active" : "") + (i === selectedDistrict ? " sel" : "");
    button.setAttribute("aria-label", DISTRICTS[i] + " district, " + status);
    button.setAttribute("aria-pressed", String(i === selectedDistrict));
  });
  const i = selectedDistrict;
  $("cityinfo").textContent = "District " + (i + 1) + ": " + DISTRICTS[i] + " · " + districtStatus(i, level) + " · planned topic: " + LEVEL_TOPICS[i];
}
function paintModes() {
  const placed = Boolean(game.placed);
  const current = game.mode === "campaign" || game.mode === "boss" ? "play" : game.mode === "drill" ? "quiz" : game.mode;
  [...$("modes").children].forEach((button) => {
    const mode = button.dataset.mode, info = MODES[mode];
    const locked = mode !== "placement" && !placed;
    button.disabled = busy || !sessionId || locked;
    button.classList.toggle("on", mode === current);
    button.classList.toggle("need", mode === "placement" && !placed && mode !== current);
    const note = locked ? "Locked: placement test first" : mode === "placement" ? (placed ? info.noteDone : info.note) : info.note;
    button.querySelector("small").textContent = note;
    button.setAttribute("aria-label", button.querySelector(".sign").textContent + ". " + note);
  });
}
function saveProgress() {
  try {
    localStorage.setItem("signalProgress", JSON.stringify({ rank: game.rank, level: game.level, tokens: game.tokens, hint: game.hint, placed: Boolean(game.placed), save: game.save || "" }));
  } catch (error) {}
}
function savedProgress() {
  try {
    const data = JSON.parse(localStorage.getItem("signalProgress") || "null");
    return data && data.placed ? data : null;
  } catch (error) { return null; }
}
function drawGame(announce, previous) {
  $("rank").textContent = String(game.rank || "—");
  $("streak").textContent = game.streak || 0;
  $("level").textContent = game.level || 1;
  const tokens = Number(game.tokens) || 0;
  if (announce && previous && tokens !== previous.tokens) countTo($("tokens"), previous.tokens || 0, tokens); else $("tokens").textContent = tokens;
  const hint = Math.max(0, Math.min(5, Number(game.hint) || 0));
  [...$("hintbars").children].forEach((bar, i) => bar.classList.toggle("on", i < hint));
  $("hintbars").setAttribute("aria-label", "Hint charge " + hint + " of 5");
  if (previous && Number(game.level) !== Number(previous.level)) selectedDistrict = -1;
  paintCity();
  document.body.classList.toggle("boss", game.mode === "boss");
  const order = "UMPIRE", at = order.indexOf(String(game.step || "").toUpperCase());
  [...$("steps").querySelectorAll("span")].forEach((el, i) => {
    el.classList.toggle("on", i === at);
    el.classList.toggle("done", at > -1 && i < at);
  });
  paintModes();
  moveWalker();
  paintBadges(announce);
  const toward = tokens % 100;
  $("xpfill").style.width = toward + "%";
  $("xptext").textContent = toward + " / 100";
  $("xptrack").setAttribute("aria-label", "Progress to next reward: " + toward + " of 100 tokens");
  const names = { placement: "PLACEMENT TEST", play: "QUEST", learn: "LESSON", quiz: "QUIZ", campaign: "QUEST", drill: "DRILL", boss: "BOSS ROUND", idle: "TOWN SQUARE" };
  const q = Number(game.q) || 0, total = Number(game.q_total) || 0;
  $("roundtitle").textContent = (names[game.mode] || "TOWN SQUARE") + (q && total ? " · " + q + " OF " + total : "");
  $("pips").textContent = "";
  for (let i = 1; i <= Math.min(total, 30); i++) {
    const pip = document.createElement("i");
    if (i < q) pip.className = "done"; else if (i === q) pip.className = "now";
    $("pips").append(pip);
  }
  if (announce) {
    if (game.result === "correct") { flash("NICE!" + (game.gain ? " +" + game.gain : ""), false); beep(660, 120); beep(990, 180, 0.12); burst(); confetti(); }
    else if (game.result === "wrong") { flash("NOT QUITE", true); beep(240, 240); shake(); }
    if (previous && seenState && Number(game.level) > Number(previous.level)) { flash("LIGHTS ON!", false); beep(523, 120); beep(659, 120, 0.12); beep(784, 240, 0.24); }
  }
}
function applyState(text, announce) {
  const next = extractState(text);
  if (!next) return;
  const previous = game;
  game = Object.assign({}, game, next);
  if (previous.placed) game.placed = true;  // the placement test is only ever taken once
  if (Number(game.streak) > bestStreak) { bestStreak = Number(game.streak); try { localStorage.setItem("signalBest", String(bestStreak)); } catch (error) {} }
  seenState = true;
  drawGame(announce, previous);
  saveProgress();
}

function startTimer() {
  stopTimer();
  timerStart = Date.now();
  timerHandle = setInterval(() => {
    const s = Math.floor((Date.now() - timerStart) / 1000);
    $("timer").textContent = String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }, 1000);
}
function stopTimer() { if (timerHandle) clearInterval(timerHandle); timerHandle = null; }

function addLog(kind, who, text) {
  const msg = document.createElement("div");
  msg.className = "msg " + kind;
  msg.innerHTML = '<div class="who2">' + who + '</div><div class="text">' + render(text) + "</div>";
  log.append(msg);
  log.scrollTop = log.scrollHeight;
}
function voiceLabel(text) {
  const match = text.match(/\[(COACH|INTERVIEWER)\]/);
  $("who").textContent = match ? "SIGNAL · " + match[1] : "SIGNAL";
  return text.replace(/\[(COACH|INTERVIEWER)\]\s*/g, "");
}
function stopTyping() { if (typing) { clearInterval(typing.timer); typing.finish(); typing = null; } }
function showCards(options) {
  cards.textContent = "";
  options.forEach((option) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "card";
    card.dataset.key = option.key;
    card.innerHTML = '<span class="key">' + option.key + "</span><span>" + inline(option.text) + "</span>";
    card.addEventListener("click", () => choose(option.key));
    cards.append(card);
  });
}
// The message types itself out like an RPG dialogue box. Click the box to skip.
function showStage(text, instant) {
  stopTyping();
  const { question, options } = splitOptions(voiceLabel(stripState(text)));
  cards.textContent = "";
  if (instant || calm || question.length < 12) { stage.innerHTML = render(question); showCards(options); return; }
  let shown = 0;
  const step = Math.max(2, Math.ceil(question.length / 90));
  const finish = () => { stage.innerHTML = render(question); showCards(options); };
  typing = { finish, timer: setInterval(() => {
    shown += step;
    if (shown >= question.length) { stopTyping(); return; }
    stage.innerHTML = render(question.slice(0, shown));
  }, 16) };
}
$("bubble").addEventListener("click", stopTyping);
function choose(key) {
  if (busy || !sessionId) return;
  const card = [...cards.children].find((c) => c.dataset.key === key);
  if (!card) return;
  [...cards.children].forEach((c) => { c.disabled = true; });
  card.classList.add("locked");
  beep(880, 70);
  send(key, true);
}
function setState(name) {
  const words = { STARTING: "WAKING UP", THINKING: "THINKING…", READY: "READY", LISTENING: "LISTENING", SPEAKING: "TALKING", OFFLINE: "OFFLINE" };
  stateEl.textContent = words[name] || name;
  const buddy = $("corebtn");
  buddy.classList.toggle("busy", name === "THINKING" || name === "STARTING");
  buddy.classList.toggle("live", name === "LISTENING");
  buddy.classList.toggle("talking", name === "SPEAKING");
  buddy.setAttribute("aria-label", name === "LISTENING" ? "Stop listening" : "Speak your answer");
  $("corehint").textContent = !recognition ? "" : name === "LISTENING" ? "TAP ME TO STOP" : "TAP ME TO SPEAK";
}
function setBusy(value) {
  busy = value;
  controls.forEach((el) => { el.disabled = value || !sessionId; });
  if (!recognition) { $("mic").disabled = true; $("corebtn").disabled = true; }
  paintModes();
  setState(value ? "THINKING" : "READY");
}

// Voice: SIGNAL reads its replies aloud using the voices installed on this computer.
let voiceOn = true, chosenVoice = "", speaking = [];
try { chosenVoice = localStorage.getItem("signalVoice") || ""; } catch (error) {}
const PREFERRED = ["Jamie (Premium)", "Jamie (Enhanced)", "Daniel (Premium)", "Daniel (Enhanced)", "Oliver (Premium)", "Oliver (Enhanced)", "Arthur (Premium)", "Arthur (Enhanced)", "Daniel", "Arthur", "Oliver", "Google UK English Male"];
function englishVoices() {
  const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
  return voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
}
function pickVoice() {
  const voices = englishVoices();
  if (chosenVoice) { const saved = voices.find((v) => v.name === chosenVoice); if (saved) return saved; }
  for (const name of PREFERRED) {
    const match = voices.find((v) => v.name === name) || voices.find((v) => v.name.startsWith(name));
    if (match) return match;
  }
  return voices.find((v) => v.lang.replace("_", "-") === "en-GB") || voices[0] || null;
}
function fillVoiceList() {
  const list = $("voicepick"), voices = englishVoices();
  if (!voices.length) return;
  const current = pickVoice();
  list.textContent = "";
  voices.slice().sort((x, y) => (y.lang.includes("GB") - x.lang.includes("GB")) || x.name.localeCompare(y.name)).forEach((v) => {
    const option = document.createElement("option");
    option.value = v.name;
    option.textContent = v.name + " · " + v.lang;
    if (current && current.name === v.name) option.selected = true;
    list.append(option);
  });
  list.disabled = false;
}
function speakable(text) {
  return stripState(text)
    .replace(/```[\s\S]*?```/g, " The code is on your screen. ")
    .replace(/\[(COACH|INTERVIEWER)\]/g, " ")
    .replace(/^\s*\**([A-D])[\).]\**\s+/gm, "$1. ")
    .replace(/[*`#_]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}
function stopSpeaking() {
  speaking = [];
  if (window.speechSynthesis && (speechSynthesis.speaking || speechSynthesis.pending)) speechSynthesis.cancel();
}
function voiceNote(text) { $("voicelog").textContent = text; }
function localVoice() {
  const voices = englishVoices();
  return voices.find((v) => v.localService && v.lang.includes("GB")) || voices.find((v) => v.localService) || null;
}
function speak(text, force, fallback) {
  if ((!voiceOn && !force) || !window.speechSynthesis) return;
  speaking = [];
  speechSynthesis.cancel();  // always reset first: Chrome's voice can get stuck
  const clean = speakable(text);
  if (!clean) return;
  setTimeout(() => {
    const voice = fallback ? localVoice() : pickVoice();
    const pieces = clean.match(/[^.!?]+[.!?]*/g) || [clean];
    let started = false;
    const mine = [];
    pieces.forEach((piece, index) => {
      if (!piece.trim()) return;
      const utterance = new SpeechSynthesisUtterance(piece.trim());
      if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
      utterance.rate = 1.0;
      utterance.pitch = 0.9;
      utterance.volume = 1;
      utterance.onstart = () => {
        started = true;
        voiceNote("Speaking with: " + (voice ? voice.name : "your computer's default voice"));
        if (!busy && !listening) setState("SPEAKING");
      };
      utterance.onerror = (event) => {
        if (event.error === "interrupted" || event.error === "canceled") return;
        if (event.error === "not-allowed") { pendingSpeech = text; $("corehint").textContent = "CLICK ANYWHERE TO HEAR ME"; voiceNote("Waiting for your first click to turn sound on."); return; }
        if (!fallback && speaking === mine) { speak(text, force, true); return; }
        voiceNote("Voice problem: " + event.error);
      };
      if (index === pieces.length - 1) utterance.onend = () => { if (!busy && !listening) setState("READY"); };
      mine.push(utterance);
      speechSynthesis.speak(utterance);
    });
    speaking = mine;
    speechSynthesis.resume();
    // If nothing has started after a moment, try again with a built-in voice.
    setTimeout(() => {
      if (started || pendingSpeech || speaking !== mine) return;
      if (!fallback) { voiceNote("First voice did not start. Trying a built-in voice."); speak(text, force, true); }
      else voiceNote("No sound started. Check your Mac's volume and Chrome's tab is not muted, then press TEST VOICE.");
    }, 2500);
  }, 150);
}
if (window.speechSynthesis) {
  fillVoiceList();
  speechSynthesis.onvoiceschanged = fillVoiceList;
  $("voice").addEventListener("click", () => {
    voiceOn = !voiceOn;
    $("voice").textContent = voiceOn ? "VOICE ON" : "VOICE OFF";
    $("voice").setAttribute("aria-pressed", String(voiceOn));
    if (voiceOn) speak("Voice online."); else stopSpeaking();
  });
  $("voicepick").addEventListener("change", () => {
    chosenVoice = $("voicepick").value;
    try { localStorage.setItem("signalVoice", chosenVoice); } catch (error) {}
    speak("Good evening. All systems are online.", true);
  });
  $("voicetest").addEventListener("click", () => { $("voicehint").textContent = ""; speak("Good evening. All systems are online.", true); });
} else {
  voiceOn = false;
  $("voice").textContent = "NO VOICE";
  $("voice").disabled = true;
  $("voicetest").disabled = true;
}

async function refreshSpend() {
  try {
    const data = await (await fetch("/api/spend")).json();
    const dollars = (cents) => "$" + (cents / 100).toFixed(2);
    $("spend").textContent = dollars(data.spent_cents) + " / " + dollars(data.cap_cents);
  } catch (error) {}
}

async function send(text, show) {
  if (busy || !sessionId || !text.trim()) return;
  stopListening();
  stopSpeaking();
  stopTimer();
  stopTyping();
  if (show) addLog("you", "YOU", text);
  setBusy(true);
  [...cards.children].forEach((c) => { c.disabled = true; });
  let reply = "";
  try {
    const response = await fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      reply += decoder.decode(value, { stream: true });
    }
  } catch (error) {
    reply += "\n[Could not reach the app: " + error.message + "]";
  }
  if (!reply.trim()) reply = "[No reply. Try sending again.]";
  addLog("agent", "SIGNAL", stripState(reply));
  setBusy(false);
  showStage(reply, false);
  applyState(reply, true);
  refreshSpend();
  startTimer();
  speak(reply);
  if (!splitOptions(stripState(reply)).options.length) input.focus();
}

$("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value;
  if (!text.trim()) return;
  input.value = "";
  send(text, true);
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("form").requestSubmit(); }
});
$("hintbtn").addEventListener("click", () => send("hint", true));
[...$("modes").children].forEach((button) => {
  button.addEventListener("click", () => {
    const info = MODES[button.dataset.mode];
    beep(600, 70);
    send(button.dataset.mode === "placement" && game.placed ? info.again : info.ask, true);
  });
});
document.addEventListener("keydown", (event) => {
  if (event.target === input || event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toUpperCase();
  if (key.length === 1 && "ABCD".includes(key)) { stopTyping(); choose(key); }
});

// Speaking: uses the browser's speech recognition (works best in Chrome).
const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Speech) {
  recognition = new Speech();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";
  let base = "";
  recognition.onstart = () => { base = input.value ? input.value + " " : ""; };
  recognition.onresult = (event) => {
    let spoken = "";
    for (const result of event.results) spoken += result[0].transcript;
    input.value = base + spoken;
  };
  recognition.onerror = (event) => { $("michint").textContent = "Mic problem: " + event.error; };
  recognition.onend = () => { listening = false; $("mic").textContent = "SPEAK"; if (!busy) setState("READY"); };
  const toggleMic = () => {
    if (listening) { stopListening(); return; }
    stopSpeaking();
    $("michint").textContent = "";
    try { recognition.start(); listening = true; $("mic").textContent = "STOP"; setState("LISTENING"); beep(740, 80); usedMic = true; try { localStorage.setItem("signalMic", "1"); } catch (error) {} paintBadges(true); }
    catch (error) { $("michint").textContent = "Mic problem: " + error.message; }
  };
  $("mic").addEventListener("click", toggleMic);
  $("corebtn").addEventListener("click", toggleMic);
} else {
  $("michint").textContent = "Speaking needs Chrome. Mac dictation still works here.";
}
function stopListening() { if (recognition && listening) recognition.stop(); }

async function boot(fresh) {
  sessionId = null;
  stopSpeaking();
  stopTimer();
  stopTyping();
  controls.forEach((el) => { el.disabled = true; });
  log.textContent = "";
  cards.textContent = "";
  stage.textContent = "Waking up SIGNAL…";
  setState("STARTING");
  try {
    const response = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fresh: Boolean(fresh) }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "could not start a session");
    sessionId = data.session_id;
    refreshSpend();
    setBusy(false);
    const hidden = (item) => item.who === "you" && (/^start\s*\n*\[HUD PROTOCOL/.test(item.text.trim()) || item.text.trim() === "start" || item.text.trim().startsWith("[SCREEN UPDATE"));
    const history = (data.history || []).filter((item) => !hidden(item));
    let protoKey = "";
    try { protoKey = localStorage.getItem("signalProto") || ""; } catch (error) {}
    const protoNow = sessionId + ":" + PROTOCOL;
    try { localStorage.setItem("signalProto", protoNow); } catch (error) {}
    if (data.resumed && history.length) {
      let lastAgent = "";
      history.forEach((item) => {
        addLog(item.who === "you" ? "you" : "agent", item.who === "you" ? "YOU" : "SIGNAL", stripState(item.text));
        if (item.who !== "you") { lastAgent = item.text; applyState(item.text, false); }
      });
      if (lastAgent) { showStage(lastAgent, true); startTimer(); } else { stage.textContent = "Welcome back. Pick a place in town to continue."; }
      if (protoKey !== protoNow) {
        const kept = savedProgress();
        send(START_MESSAGE.replace(/^start/, "[SCREEN UPDATE: her game screen was just updated. Keep ALL progress and continue exactly where we were. If a question is open, repeat that same question. Do not restart anything and do not restart the placement test." + (kept ? " Her saved progress: " + JSON.stringify(kept) + "." : "") + "]"), false);
      }
    } else {
      const now = new Date().toLocaleString("en-US", { weekday: "long", hour: "numeric", minute: "2-digit" });
      const kept = savedProgress();
      const restore = kept ? "\n[SAVED PROGRESS from her earlier sessions: " + JSON.stringify(kept) + ". Restore it: use these values, treat the placement test as already done, and write them to progress.md. Do not make her retake the placement test unless she asks.]" : "";
      send(START_MESSAGE + restore + "\n[Her local time is " + now + ". Greet her accordingly.]", false);
    }
  } catch (error) {
    setState("OFFLINE");
    stage.textContent = "[Could not start: " + error.message + "]\nCheck that ANTHROPIC_API_KEY is set in the terminal, then reload this page.";
  }
}
document.addEventListener("click", () => {
  if (!pendingSpeech) return;
  const text = pendingSpeech;
  pendingSpeech = "";
  speak(text);
}, true);
buildCity();
buildDecor();
drawGame(false, null);
$("fresh").addEventListener("click", () => { if (!busy) boot(true); });
boot(false);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if "PASTE_YOUR" in AGENT_ID or "PASTE_YOUR" in ENVIRONMENT_ID:
        print("Open signal_town.py and paste your agent ID and environment ID near the top first.")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print('No API key found. Run this first:  export ANTHROPIC_API_KEY="your-key-here"')
        sys.exit(1)
    print(f"SIGNAL HUD is running. Open http://127.0.0.1:{PORT} in Chrome. Press Ctrl+C to stop.")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
