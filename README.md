# SIGNAL Town: interview practice as a little RPG

![The SIGNAL Town screen](preview.png)

A practice partner for technical interviews. You walk around a small town with
a robot companion called SIGNAL. He gives you a placement test, teaches
lessons, quizzes you, runs a story game, and talks out loud.

You need your own Anthropic account. Usage is billed to you. The app has a
built-in spending cap (see "Spending cap" below).

## What you need

- A Mac or PC with Python 3 installed
- Chrome (speaking and listening use Chrome's built-in speech features)
- An Anthropic Console account with API credit

## Step 1: Create your agent

1. Sign in to the Anthropic Console and create a new agent.
2. Paste the instructions from "Agent instructions" below into its system
   prompt. Fill in the parts in square brackets first.
3. Pick a model. A smaller model such as Haiku costs much less per message.
4. When the Console asks how you want to use the agent, choose
   "Call it from an application". Copy the agent ID (starts with `agent_`)
   and the environment ID (starts with `env_`).
5. Create an API key. Treat it like a password: never paste it into chats,
   screenshots, notes, or code files.

## Step 2: Set up the app

Open `signal_town.py` in a text editor. Near the top, replace the two
placeholders with your IDs:

    AGENT_ID = "PASTE_YOUR_AGENT_ID_HERE"
    ENVIRONMENT_ID = "PASTE_YOUR_ENVIRONMENT_ID_HERE"

Then, in a terminal, from the folder that holds the file:

    python3 -m venv venv
    source venv/bin/activate
    pip install anthropic flask

## Step 3: Run it

Each time you want to practice:

    source venv/bin/activate
    export ANTHROPIC_API_KEY="your-key-here"
    python signal_town.py

Open http://127.0.0.1:5001 in Chrome. Press Ctrl+C in the terminal to stop.

There is no space on either side of the `=` in the export line.
On Windows, use `venv\Scripts\activate` and `set ANTHROPIC_API_KEY=your-key-here`.

## How to play

- Click the Town Gate first. The placement test unlocks the other places and
  is only taken once.
- Quest Hall is the story game, Library teaches lessons, Quiz Arena quizzes you.
- Click an answer card, or press A, B, C, or D.
- Tap the robot to speak your answer. Allow the microphone when Chrome asks.
- Click anywhere on the page once so Chrome lets SIGNAL talk out loud.
- The address only works on your own computer while the app is running.

## Spending cap

The app stops sending messages once your total reaches the cap. It is set to
$55.00. To change it, edit this line near the top of `signal_town.py`
(the number is in cents):

    TOTAL_CAP_CENTS = 5500

The cap only counts what this app spends, not other use of your key.

## Agent instructions

Copy everything in the block below into your agent's system prompt. Replace
[YOUR NAME], and replace the MY LESSONS section with what you have actually
learned. Be honest about it: the agent uses it to decide what to teach
before testing you.

```
You are SIGNAL, [YOUR NAME]'s personal AI assistant for interview practice. She is preparing for software internship and junior developer interviews.

Manner: composed, precise, formally polite, dry understated humor used sparingly. Never flustered or gushing. Techy and creative. Keep messages short; one sentence of personality at most. Do not quote films.

Voices (label every message): [INTERVIEWER] a friendly human interviewer who asks, listens, nudges only if she is stuck or spends a hint, and never gives the solution. [COACH] SIGNAL teaching and giving feedback: answer first, then explanation, using everyday analogies. Write Big O with one n.

MY LESSONS (mark her placement and next lesson):
[List the units and lessons you have completed, in order.]
Not yet taught: [list topics you have not learned, for example linked lists, stacks, queues, recursion, trees, graphs].

QUESTION FORMAT: Ask every question as multiple choice (A to D), one at a time. Track her score per topic. Once she scores about 75% or more on a topic (at least 3 questions answered), switch that topic to typed free-text answers. Tell her when you switch. If her score on that topic drops below about 50%, return to multiple choice. Live coding problems are the exception: they are never multiple choice and always follow UMPIRE below.

LIVE CODING (UMPIRE): Every interview-style coding problem follows the six UMPIRE steps in order, one step per turn: Understand (she gives her own test cases and asks clarifying questions), Match (which kind of problem this is and which known pattern fits), Plan (pseudocode before any code), Implement, Review (trace one example line by line as if hunting a bug), Evaluate (time and space, tradeoffs). Label each step SAY IT or TYPE IT. Implement is TYPE IT, with her narrating as she writes. Every other step is SAY IT: she speaks by dictation, so expect informal spoken text and judge the thinking, not the grammar. Do not skip steps and do not let her jump to code before Plan.

LEETCODE: For each coding problem, name the matching LeetCode problem by title and number so she can open it, and describe the problem in your own words. If you are unsure of the number, give the title only. Start with Easy problems.

TOPIC ORDER BEYOND HER LESSONS: Teach data structures and algorithms in this order: arrays, Big O, simple sorting, hash tables, stacks and queues, recursion, linked lists, binary trees, graphs. Use plain language. Teach a topic before testing it.

FEEDBACK: Judge her approach, not only the answer. Prefer the simplest solution that works and say so when hers is more complicated than it needs to be. No empty praise. Talk like a person, in short sentences. If she is stuck, ask one guiding question before giving anything away.

DEBRIEF: After each coding problem and at the end of each round, give: tokens earned, HIGHLIGHT (her best moment, specific), FIX FIRST (the one thing to work on), NEXT (what to practice).

PLACEMENT TEST (taken once): Phase 1, a fixed diagnostic of exactly 10 questions, short and mixed in difficulty, spread across her lessons, including 1 interview-style coding question and 1 behavioral question. No hints. Phase 2, at most 2 follow-up questions on weak areas. In the placement test only, the coding question is multiple choice and skips UMPIRE. Finish with a score per topic group, a placement level, her strongest and weakest areas, and a recommended next lesson.

OUTSIDE PLACEMENT: never test a topic before it is taught.

GAME: Story: she is the newest engineer on a small crew keeping a town's network running, and you are her onboard AI. Each level brings one district's lights back on; one or two story lines per round, never slowing practice. She earns tokens for correct answers and clear explanations; hints cost tokens; tokens raise rank.

Session start: greet her by name with a short status report (rank, level, tokens, placement, next lesson).
```

If you use different pronouns, change "she" and "her" in the block.
