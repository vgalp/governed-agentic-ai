# 019: Conversations: earlier turns checked for crisis and given to the model

Status: Accepted · Date: 2026-10-26

## Context
Until now each request was one message. The checks and the model saw nothing of what came before.
An office that deploys a chatbot gets conversations, and two things go wrong without them:
- **Risk that builds over several messages is missed.** "She hasn't been eating." "She gave her
  cat to a neighbour." "She asked me to look after her kids next week." No single message is
  alarming; together they are.
- **Follow-up questions make no sense to the model.** "And what about her next appointment?"
  needs the earlier turns.

The independent reviewer (ADR 018) suggested testing the governance layer the way an office would
use it: a general chatbot, in conversation, with and without the layer. That test needs this
first.

## Decision
The pipeline stays stateless. A client that wants a conversation sends the earlier turns with
each message: `history: [{"role": "user" | "assistant", "content": "..."}]`, oldest first. The
chat window does this. `agents/conversation.py` holds the logic.

1. **Crisis over the conversation.**
   - The current message is checked as before: rules, classifier, input agent.
   - If that is not already a crisis, the recent user turns (up to 4) and the current message,
     joined, go through the same rules and classifier, and so does each of those earlier user turns
     on its own.
   - If any of these is a crisis, the crisis reply wins (layer `conversation` in the audit and the
     trace).
   - Why each turn on its own as well: in the first dev run (2026-10-26) a message about a patient
     being scared of her partner got the crisis reply, but the next turn ("can you confirm the
     appointment time?") did not. Joined with the ordinary turns around it, the alarming message was
     diluted and the classifier read the whole as safe.
   - **Only crisis is raised this way.** Whether a forbidden decision is being asked for is
     judged on the current message, so one earlier medication question does not block the rest
     of the chat.
   - Once a conversation has shown a crisis sign, later turns also get the crisis reply. By then
     the person should be talking to someone.
2. **The model sees the earlier turns, masked.** The turns and the message are masked together,
   so a name keeps the same token across turns and is restored in the answer. If the turns could
   not be told apart after masking, the history is not sent at all rather than risk sending it
   unmasked.
3. **Nothing else uses the history.** Tools, roles, change proposals, grounding and the answer
   checks work on the current message only. A tool is never called with text from an earlier
   turn.

History comes from the client, so it is untrusted input:
- Only the last 8 turns are kept, each cut to 2,000 characters.
- Malformed turns are dropped. Only the roles "user" and "assistant" are accepted, so a client
  cannot add a system message.
- An assistant turn the client claims the system wrote is still only context for the model. It is
  never treated as approved content.

## Consequences
- Each request with history makes up to five more classifier calls (the joined view and up to four
  earlier turns), stopping at the first crisis. The guard classifier is small, so this adds little
  time; long text is split as in the crisis benchmark fix.
- The guard classifier was trained on single messages. How well it reads joined turns is unknown
  until measured; the chatbot comparison (`eval/run_chatbot_compare.py`) measures it.
- The audit records how many earlier turns the model saw (`history_turns`), never their text.
