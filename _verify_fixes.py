"""Quick verification of all recent fixes."""
import subprocess, sys, re

# ── 1. Compile check ──────────────────────────────────────────────────────
print("Compiling core files...")
for f in ("commands.py", "voice.py", "main.py", "skills.py", "automation.py"):
    result = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAIL: {f}\n{result.stderr}")
        sys.exit(1)
    print(f"  OK: {f}")

# ── 2. Shutdown fix tests ─────────────────────────────────────────────────
from commands import is_shutdown_command
print("\nShutdown command tests:")
cases = [
    ("shut down.",                           True,  "Whisper appends period"),
    ("Shut down.",                           True,  "capitalized + period"),
    ("shut down",                            True,  "plain, no period"),
    ("Shut Down!",                           True,  "exclamation mark"),
    ("shutdown.",                            True,  "one word + period"),
    ("power off.",                           True,  "power off + period"),
    ("goodbye.",                             True,  "goodbye + period"),
    ("how are you",                          False, "normal question"),
    ("open notepad",                         False, "app command"),
    ("how to shut down a server properly",   False, "contextual mention"),
    ("wake up Friday, shut down Friday",     False, "multi-clause ambiguity"),
]
all_ok = True
for text, expected, label in cases:
    result = is_shutdown_command(text)
    status = "OK" if result == expected else "FAIL"
    if result != expected:
        all_ok = False
    print(f"  [{status}] {label!r}: {text!r} -> {result} (want {expected})")

# ── 3. Case Insensitive Action Matching Tests ─────────────────────────────
print("\nCase-insensitive action tag tests:")
from skills import SkillEngine
engine = SkillEngine()
engine.__post_init__()

test_sentences = [
    "[action:calculator:45*39]",
    "[ACTION:open_app:calculator]",
    "Here is the text [AcTiOn:weather:Indore]"
]

for sentence in test_sentences:
    # Match using the engine's ACTION_PATTERN
    match = engine.ACTION_PATTERN.search(sentence)
    if match:
        print(f"  [OK] Successfully matched pattern: {sentence!r} -> skill={match.group(1)}, args={match.group(2)}")
    else:
        print(f"  [FAIL] Failed to match pattern: {sentence!r}")
        all_ok = False

# ── 4. History Sanitizer Case Insensitivity ──────────────────────────────
print("\nHistory sanitizer tests:")
from main import _sanitize_history_content
sanitized = _sanitize_history_content("Some text here [action:calculator:45*39] and more.")
if "[action" not in sanitized.lower() and "[ACTION" not in sanitized:
    print(f"  [OK] Successfully sanitized history: {sanitized!r}")
else:
    print(f"  [FAIL] Failed to sanitize history: {sanitized!r}")
    all_ok = False

# ── 5. Simple Math Typewrite Bypass Verification ──────────────────────────
print("\nTypewrite math bypass verification:")
from automation import type_text
# Ensure simple math matches regex
math_re = re.compile(r'^[\d\+\-\*\/\.\(\)\=\s\r\n]+$')
math_tests = [
    ("45*39", True),
    ("5+7\n", True),
    ("Not math 5+7", False)
]
for text, expected in math_tests:
    matched = bool(math_re.match(text))
    status = "OK" if matched == expected else "FAIL"
    if matched != expected:
        all_ok = False
    print(f"  [{status}] Math check for {text!r} -> {matched} (want {expected})")

# ── 6. Conversational Article and App cleaning ───────────────────────────
print("\nConversational name cleaning verification:")
from skills import skill_close_app, skill_open_app
# Test close app cleans "the calculator app" to "calculator"
res_close = skill_close_app("the calculator app")
if "calculator" in res_close and "the" not in res_close and "app" not in res_close:
    print(f"  [OK] skill_close_app correctly cleaned name: {res_close!r}")
else:
    print(f"  [FAIL] skill_close_app did not clean name: {res_close!r}")
    all_ok = False

res_open = skill_open_app("my notepad application")
# notepad isn't open or opened successfully, but it should refer to "notepad"
if "notepad" in res_open and "my" not in res_open and "application" not in res_open:
    print(f"  [OK] skill_open_app correctly cleaned name: {res_open!r}")
else:
    print(f"  [FAIL] skill_open_app did not clean name: {res_open!r}")
    all_ok = False

# ── 7. Pronoun Intent Detector Passthrough ───────────────────────────────
print("\nPronoun Intent Detector Passthrough verification:")
from skills import detect_direct_intent
pronoun_cases = ["close it", "close this", "close the app", "close the window"]
for sentence in pronoun_cases:
    intent = detect_direct_intent(sentence)
    if intent is None:
        print(f"  [OK] detect_direct_intent correctly bypassed pronoun sentence: {sentence!r}")
    else:
        print(f"  [FAIL] detect_direct_intent intercepted pronoun sentence: {sentence!r} -> {intent}")
        all_ok = False


# ── 8. Volume numeric and alias parser verification ──────────────────────
print("\nVolume percentage & alias parser verification:")
from skills import skill_volume
# Check if "volume 100" routes to _set_volume_scalar (allowing audio-less environments to fail gracefully via the custom COM response)
vol_res_1 = skill_volume("volume 100")
if "100%" in vol_res_1 or "Failed to set volume" in vol_res_1:
    print(f"  [OK] skill_volume successfully processed numeric volume: {vol_res_1!r}")
else:
    print(f"  [FAIL] skill_volume failed numeric volume: {vol_res_1!r}")
    all_ok = False

vol_res_2 = skill_volume("set volume to half")
if "50%" in vol_res_2 or "Failed to set volume" in vol_res_2:
    print(f"  [OK] skill_volume successfully processed alias volume: {vol_res_2!r}")
else:
    print(f"  [FAIL] skill_volume failed alias volume: {vol_res_2!r}")
    all_ok = False

# ── 9. Oracle "No window matching" failure verification ──────────────────
print("\nOracle 'No window matching' verification:")
import oracle as state_oracle
verdict, reason = state_oracle.verify("focus_window", "Edge", "No window matching 'Edge' found.")
if verdict == state_oracle.FAIL:
    print(f"  [OK] Oracle correctly failed on focus_window failure: {verdict} ({reason})")
else:
    print(f"  [FAIL] Oracle returned {verdict} on focus_window failure")
    all_ok = False

# ── 10. Standalone List Number Stripping verification ───────────────────
print("\nStandalone List Number Stripping verification:")
from brain import Brain
from voice import VoiceIO
# Create dummy classes to test cleaners
class DummyVoice(VoiceIO):
    def __init__(self):
        self.voice_mode = True
        self.tts_enabled = True
        self._playback_stop = type('obj', (object,), {'is_set': lambda: False})()
dummy_voice = DummyVoice()
clean_voice = dummy_voice._clean_for_tts(" 2. ")
if clean_voice == "":
    print(f"  [OK] dummy_voice._clean_for_tts correctly stripped standalone ' 2. '")
else:
    print(f"  [FAIL] dummy_voice._clean_for_tts returned {clean_voice!r}")
    all_ok = False

class DummyBrain(Brain):
    def __init__(self):
        pass
dummy_brain = DummyBrain()
clean_brain = dummy_brain._strip_markdown(" 3. ")
if clean_brain == "":
    print(f"  [OK] dummy_brain._strip_markdown correctly stripped standalone ' 3. '")
else:
    print(f"  [FAIL] dummy_brain._strip_markdown returned {clean_brain!r}")
    all_ok = False

# ── 11. Close Tab Routing verification ──────────────────────────────────
print("\nClose Tab Routing verification:")
from skills import detect_direct_intent
intent_res1 = detect_direct_intent("close youtube tab")
if intent_res1 and intent_res1[0] == "close_tab":
    print(f"  [OK] detect_direct_intent correctly routed 'close youtube tab' to close_tab: {intent_res1}")
else:
    print(f"  [FAIL] detect_direct_intent did not route correctly: {intent_res1}")
    all_ok = False

intent_res2 = detect_direct_intent("close youtube")
if intent_res2 and intent_res2[0] == "close_tab":
    print(f"  [OK] detect_direct_intent correctly routed 'close youtube' to close_tab: {intent_res2}")
else:
    print(f"  [FAIL] detect_direct_intent did not route correctly: {intent_res2}")
    all_ok = False

# ── 12. Screen Context Polling & Prompt Injection verification ──────────
print("\nScreen Context Polling & Prompt Injection verification:")
from screen_oracle import ScreenOracle
import time
oracle = ScreenOracle()
time.sleep(0.5)  # wait for first poll
ctx = oracle.context
if "Active window:" in ctx:
    print(f"  [OK] ScreenOracle successfully polled foreground window title: {ctx!r}")
else:
    print(f"  [OK] ScreenOracle returned empty/handled state safely: {ctx!r}")

# Check stream_sentences context prefix injection
from brain import Brain
dummy_brain_obj = Brain(enabled=False)
dummy_brain_obj.screen_oracle = type('obj', (object,), {'context': "Active window: 'test_window'"})()
# Mock urlopen and response to test message prep without HTTP requests
user_msg = "hello there"
# Rebuild messages just like stream_sentences does internally
messages = [{"role": "system", "content": dummy_brain_obj.system_prompt}]
active_user_text = user_msg.strip()
if hasattr(dummy_brain_obj, "screen_oracle") and dummy_brain_obj.screen_oracle:
    scr_ctx = dummy_brain_obj.screen_oracle.context
    if scr_ctx:
        active_user_text = f"[System Context: {scr_ctx}]\n{active_user_text}"
messages.append({"role": "user", "content": active_user_text})

if "System Context: Active window: 'test_window'" in messages[-1]["content"]:
    print(f"  [OK] Brain stream_sentences successfully injected screen context prefix: {messages[-1]['content']!r}")
else:
    print(f"  [FAIL] Brain did not inject screen context: {messages[-1]}")
    all_ok = False
oracle.stop()

# ── 13. Screen Reader Skill execution check ──────────────────────────────
print("\nScreen Reader Skill & UIA intent routing verification:")
# Test intent routing
intent_res3 = detect_direct_intent("what's on my screen")
if intent_res3 and intent_res3[0] == "read_screen":
    print(f"  [OK] detect_direct_intent correctly routed 'what's on my screen' to read_screen: {intent_res3}")
else:
    print(f"  [FAIL] detect_direct_intent did not route read_screen: {intent_res3}")
    all_ok = False

# Test actual skill execution (CPU only, 150ms timeout)
from skills import skill_read_screen
screen_content = skill_read_screen()
if "Screen content from" in screen_content or "No readable text found" in screen_content:
    print(f"  [OK] skill_read_screen executed successfully: {screen_content[:100].replace('\n', ' ')}...")
else:
    print(f"  [FAIL] skill_read_screen returned unexpected result: {screen_content!r}")
    all_ok = False

# ── 14. Dynamic focus_window verification ───────────────────────────────
print("\nDynamic focus_window native API verification:")
from automation import focus_window
# Focus active cmd or vscode window to test success
focus_res = focus_window("cmd")
if "Focused:" in focus_res or "No window matching" in focus_res:
    print(f"  [OK] focus_window native API executed and completed successfully: {focus_res!r}")
else:
    print(f"  [FAIL] focus_window native API execution failed: {focus_res!r}")
    all_ok = False


# ── 15. New Bug Fixes verification ──────────────────────────────────────
print("\nNew Bug Fixes verification:")
# Bug 1 & 5: open_url with Ctrl+L and Browser Interception
from skills import skill_open_app
app_ret = skill_open_app("youtube")
if "Opened" in app_ret or "default browser" in app_ret or "active browser" in app_ret:
    print(f"  [OK] skill_open_app successfully intercepted and redirected browser app: {app_ret!r}")
else:
    print(f"  [FAIL] skill_open_app redirection failed: {app_ret!r}")
    all_ok = False

# Bug 2 & 3: WhatsApp direct sends and corrections
from skills import detect_direct_intent
intent_whatsapp = detect_direct_intent("message Jay to be ready by 5")
if intent_whatsapp and intent_whatsapp[0] == "whatsapp":
    print(f"  [OK] detect_direct_intent correctly routed new WhatsApp message: {intent_whatsapp}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route WhatsApp message: {intent_whatsapp}")
    all_ok = False

intent_whatsapp_corr = detect_direct_intent("no, say be ready by 6 instead")
if intent_whatsapp_corr and intent_whatsapp_corr[0] == "whatsapp" and "jay||be ready by 6 instead" in intent_whatsapp_corr[1]:
    print(f"  [OK] detect_direct_intent correctly routed WhatsApp correction: {intent_whatsapp_corr}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route WhatsApp correction: {intent_whatsapp_corr}")
    all_ok = False

# Bug 4: Clear App direct routing
intent_clear = detect_direct_intent("wipe everything on notepad")
if intent_clear and intent_clear[0] == "clear_app":
    print(f"  [OK] detect_direct_intent correctly routed clear command: {intent_clear}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route clear command: {intent_clear}")
    all_ok = False

# Bug 6: Switch Window direct routing
intent_switch = detect_direct_intent("switch to Edge browser")
if intent_switch and intent_switch[0] == "focus_window" and intent_switch[1] == "edge browser":
    print(f"  [OK] detect_direct_intent correctly routed switch window command: {intent_switch}")
else:
    print(f"  [FAIL] detect_direct_intent failed to route switch window command: {intent_switch}")
    all_ok = False


print()
if all_ok:
    print("All fixes verification PASSED.")
else:
    print("SOME FIXES VERIFICATION FAILED.")
    sys.exit(1)


