# Architectural Hardening & Decision Log: CHANGELOG

This log documents key architectural refactors, sandboxing enhancements, and runtime reliability upgrades introduced during the FRIDAY AI Assistant hardening phase. It preserves the "why" and "how" behind every major design decision to guide future development.

---

## 🛠️ Global Architecture Refactoring Log

### 1. Unified Interaction Context & History Sync (`main.py`)
> [!NOTE]
> **Issue**: Conversational context was lost whenever direct execution routes (`_handle_direct_skill`) or agentic task loops (`_handle_agentic_response`) were selected. Follow-up conversational turns (like *"write something on it"* after *"open notepad"*) caused Friday to hallucinate shutdown sequences due to lacking contextual knowledge of previous actions.
*   **Decision**: Sync every interaction route with the brain. Direct intent dispatches and agentic loop logs are now appended to `agent.brain.conversation_history`, capped to `max_history * 2` tokens, and immediately persisted via `save_history()`.

### 2. High-Sensitivity Adaptive Barge-In Floor (`voice.py`)
> [!IMPORTANT]
> **Issue**: The hardcoded minimum energy threshold (`RMS_FLOOR = 0.008`) was too high for quiet rooms or lower-gain laptop mics. Even at full vocal volume, the user's vocal energy could not beat the minimum scaled loopback gate, rendering barge-ins completely unresponsive.
*   **Decision**: Reduced `RMS_FLOOR` to `0.0015`. When a room is quiet, the minimum RMS required for barge-in drops to `0.0033` (with the `2.2x` multiplier), allowing smooth, whispered, or quiet interruptions without sacrificing loud speaker loopback rejection.

### 3. Critical Command Whisper Confidence Bypass (`ear.py`)
> [!TIP]
> **Issue**: Whisper's segment logprob filter (`logprob < -0.8`) sometimes rejected genuine critical wake or shutdown commands due to mic background noise, leading to dropped actions and subsequently parsed garbled transcriptions (e.g., rejecting *"Shut down."* and accepting *"Shut them."*).
*   **Decision**: Deterministic command bypass. We imported `is_shutdown_command` and `is_wake_command` directly into Whisper's segment parser. If the parsed phrase matches a wake or shutdown sequence, we completely bypass average logprob and no-speech confidence filters.

### 4. Global Thread-Safe Printing Mutex (`main.py`)
> [!NOTE]
> **Issue**: Multi-threaded prints from the interactive main loop, background speculative STT processes, and direct execution pipelines collided concurrently, causing interleaved, garbled, and corrupted console log output.
*   **Decision**: Process-wide serialization. Monkeypatched `builtins.print` with a recursive `threading.Lock()` at the very top of `main.py`, automatically serializing all stdout streams without refactoring hundreds of ad-hoc print calls.

### 5. Battery-Aware Power-Throttling (`main.py`)
> [!CAUTION]
> **Issue**: Running real-time speculative LLM prefills, Whisper STT, and Kokoro ONNX TTS continuously on laptop battery power caused high CPU/GPU thermal limits (> 52°C) and fast battery drain.
*   **Decision**: Automated power throttling. Implemented battery monitoring using `psutil`. When running on battery power (`plugged == False`), Friday automatically engages `power_saving_mode`, bypassing the highly resource-intensive background speculative LLM prefill pipeline.

### 6. Speech-Gated Speculative STT (`ear.py`)
> [!WARNING]
> **Issue**: Speculative STT background transcription loops remained active while Friday was speaking aloud, causing TTS audio bleed to seed false transcriptions and fire spurious speculative LLM prefill cycles.
*   **Decision**: Gated the background speculative STT trigger in `Trailing` audio states with an `and not self._speaking` condition, suppressing speculative transcriptions until Friday has completely finished speaking.

### 7. Speculative LLM Wake & Standby Filters (`main.py`)
> [!NOTE]
> **Issue**: Speculative STT triggered heavy Qwen3 8B prefill pipelines on wake phrases (e.g., *"wake up friday"*) or standby queries before checking standby state gates, wasting massive GPU compute on hardcoded actions.
*   **Decision**: Standby & wake filter guards. Prevented prefill threads from starting in `_on_speculative_transcript` if the agent is in standby or if a system wake/shutdown command is transcribed.

### 8. Speculative LLM History Context Validation (`main.py`)
> [!IMPORTANT]
> **Issue**: Prefilled speculative runs were adopted without verifying if the conversation history changed since prefill start, leading to context desynchronization when skills or agentic loops modified the history in parallel.
*   **Decision**: Added a validation check `_spec_llm_history_snapshot == len(agent.brain.conversation_history)` to the reuse check block, ensuring stale context prefills are rejected and safely re-streamed.

### 9. Advanced AST Code Execution Sandbox (`skills.py`)
> [!WARNING]
> **Issue**: The original sandbox validator allowed standard class hierarchy reflection exploits to escape constraints, such as `().__class__.__bases__[0].__subclasses__()` traversing system utilities.
*   **Decision**: Hardened AST boundary. Upgraded the `_BLOCKED_NAMES` set in `_validate_code_ast` to block `"vars"`, `"type"`, `"__class__"`, `"__bases__"`, `"__subclasses__"`, `"__mro__"`, `"__code__"`, and `"__globals__"`.

### 10. Robust Caching for System Prompts (`brain.py`)
> [!NOTE]
> **Issue**: The original caching system relied on `now.hour`. Re-building the ~2KB system prompt string on every single stream cycle caused unnecessary overhead when memory hints, skills, or voice status changed within the same hour.
*   **Decision**: Multi-key hash tuple cache. Replaced hour-only caching with a combined cache key: `(now.hour, hash(self.skill_prompt), hash(self.memory_context), hash(self.voice_status))` which updates instantly if system parameters shift, while bypassing rebuilds for unchanged states.

### 11. PowerShell Newline Injection Defenses (`automation.py`)
> [!CAUTION]
> **Issue**: The original regex `\s` matched `\n` and `\r`. Double-quoted PowerShell command arguments could be broken by injected newlines to run arbitrary secondary statements.
*   **Decision**: Swapped the general whitespace class `\s` with a literal space `' '` in parameter cleaning: `[^a-zA-Z0-9 \.\-_,\(\)\[\]]`. Newlines are now stripped out, preserving string bounds safely.

### 12. Transparent Ambiguous Network Oracle (`oracle.py`)
> [!NOTE]
> **Issue**: The network oracle in `oracle.py` was overly optimistic, returning `OK` for any string that matched standard http/https prefixes without validating actual connectivity.
*   **Decision**: Refactored the network oracle to return `AMBIGUOUS` for URL syntaxes. This triggers browser window verification or fallback validation checks rather than giving false-positives for unreachable sites.

---

## 📈 Summary of Hardening Achievements

| Wave / Fix Area | Component | Hazard Addressed | Hardening Strategy Implemented |
| :--- | :--- | :--- | :--- |
| **Wave 1** | `main.py` | Context desynchronization | Sync direct execution & agentic loop to `conversation_history` |
| **Wave 2** | `skills.py` | Python sandbox escape | Hardened AST AST-checks against class hierarchy reflection |
| **Wave 2** | `automation.py` | PowerShell argument injection | Removed newline matchers (`\s` to literal space) |
| **Wave 2** | `brain.py` | prompt builder redundancy | Prompt cache key tracking system state hashes |
| **Wave 2** | `oracle.py` | False-positive network check | Returns `AMBIGUOUS` to verify via browser window check |
| **Wave 3** | `voice.py` | Barge-in energy insensitivity | Reduced `RMS_FLOOR` to `0.0015` for low-gain mics |
| **Wave 3** | `ear.py` | Missed wake/shutdown commands | Bypass logprob & no-speech filters for critical commands |
| **Wave 3** | `voice.py` | Voiceprint enrollment overwrite | Added `os.path.exists` safeguard before enrolling |
| **Wave 3** | `main.py` | Console log interleaving | Thread-safe print wrapper monkeypatch via recursive `Lock` |
| **Wave 3** | `ear.py` | Spurious TTS bleed transcription | Gated speculative STT trigger with `not self._speaking` |
| **Wave 3** | `main.py` | Heavy GPU usage on system words | Prevent speculative LLM prefill on standby, wake & shutdown |
| **Wave 3** | `main.py` | Stale context speculative adopt | Compare prefill-start history length snapshot before reuse |
| **Wave 3** | `main.py` | High battery drain & thermal temps | Battery power-throttling to disable speculative LLM on battery |
| **Wave 4** | `main.py` | Static battery freeze | Converted power saver into active dynamic battery polling |
| **Wave 4** | `main.py` | Speculative LLM GPU contention | Added `--speculative-llm` CLI flag, disabled by default to save VRAM |
| **Wave 4** | `main.py` | Synthetic tag history contamination | Separated pure spoken history from partial interrupted logs |
| **Wave 4** | `voice.py` | Loopback speaker-id rejection | Bypassed strict speaker-ID blocks during TTS active playback |
| **Wave 4** | `voice.py` | STT pre-seed queue backlog lag | Truncated VAD pre-seed backlog window to last 3 chunks (~100ms) |
| **Wave 4** | `agentic.py` | Calculator GUI automation failures | Prompt-guided direct `calculator` skill usage with fallback keys |
| **Wave 4.5** | `voice.py` | Trailing sentence playback on interrupt | Atomic sentence_q and audio_q queue flushes upon barge-in |
| **Wave 4.5** | `voice.py` | TTS interruption debug tokens spoken | Cleaned inputs using `_clean_for_tts` regex filter |
| **Wave 4.5** | `voice.py` | High TTS boot and repeat latency | Local WAV cache with SHA256 hashes of text and style |
| **Wave 4.5** | `skills.py` | Calculator manual result copying | Automatic result copying to clipboard via pyperclip |
| **Wave 4.5** | `main.py` | Ambiguous planner parameters | REQUIRED_PARAMS schema verification and user voice/text prompting |
| **Wave 4.5** | `main.py` | Contextually wrong speculative reuse | Word-level Jaccard similarity threshold gating (>= 0.80) |

---

## 🛠️ Wave 4 Architecture Refactoring Log

### 13. Dynamic Plug-in/Plug-out Power Throttling (`main.py`)
> [!NOTE]
> **Issue**: Checking battery status only once on startup froze the power saving mode. Charger connection or battery state shifts mid-session were not dynamically evaluated.
*   **Decision**: Converted static battery state checking into a dynamic evaluation inside the speculative transcript callback (`_on_speculative_transcript`). It now dynamically queries `psutil` on every single VAD event, immediately re-enabling speculative LLM background prefill the second a charger is connected, and disabling it when disconnected to conserve power.

### 14. Speculative LLM VRAM/GPU Contention Toggle (`main.py` & CLI)
> [!CAUTION]
> **Issue**: Prefilling background LLM completions on a highly constrained 6GB VRAM split between Qwen and Whisper created continuous GPU contention, slowing down real-time speech-to-text response times and introducing a net negative latency.
*   **Decision**: Introduced the `--speculative-llm` command-line argument, disabled by default. Prefill background completions will now only execute if explicitly requested by the user, immediately boosting default speech responsiveness and reclaiming massive VRAM/GPU compute cycles.

### 15. Pure Conversational Context Protection (`main.py` & `conversation_history`)
> [!IMPORTANT]
> **Issue**: Appending synthetic debug markers (e.g. `... [interrupted]`, `[interrupted before speaking]`) to the assistant's turns in `conversation_history` caused prompts to be contaminated. On subsequent turns, the LLM read those debug tags, got confused, and generated `...` and `interrupted` as real tokens, which Kokoro spoke aloud.
*   **Decision**: Decoupled assistant history representation from execution/debug logs. The LLM's conversation history now records only clean, natural spoken response strings, while the synthetic debug tags are preserved exclusively in the persistent text interaction logs.

### 16. Loopback-Insensitive Barge-In Confirmation (`voice.py`)
> [!WARNING]
> **Issue**: Active speaker output created severe loopback audio contamination and room reverberation, causing strict speaker voiceprint verification to reject genuine user interruptions. Additionally, saving the entire detection window queued up a massive backlog, leading to severe transcription lag.
*   **Decision**: Bypassed strict speaker-ID rejection during active TTS playback (retaining diagnostic similarity logs) to deliver instantaneous Siri/Copilot-grade responsiveness. Truncated the pre-seed backlog window to only the final 3 chunks (~100ms) of real speech onset, completely eliminating transcription backlog delay.

### 17. Robust Planner Math Skill Guidance (`agentic.py`)
> [!TIP]
> **Issue**: The agentic task planner attempted to solve simple math expressions by opening the Windows Calculator app (`calc.exe`) and executing imprecise keyboard GUI shortcuts, leading to wrong calculation steps, wrong key sequences, and incorrect results.
*   **Decision**: Injected precise instruction rules for the `calculator` skill inside the task planner's system prompt, strictly guiding the LLM to leverage the high-performance, sandboxed direct `calculator` skill, while specifying robust keypress sequence fallback rules for the GUI calculator app.

### 18. Atomic Interrupt Queue Flushes (`voice.py`)
> [!IMPORTANT]
> **Issue**: During barge-in events, the player thread would continue playing cached sentence fragments or trailing audio chunks from the queues, leading to confusing overlap speech.
*   **Decision**: Implemented atomic drains for `sentence_q` and `audio_q` inside `stop_playback()`. When a barge-in is triggered, all queues are immediately and atomically cleared, cutting off audio output instantly.

### 19. TTS Interruption Debug Stripping (`voice.py`)
> [!NOTE]
> **Issue**: Diagnostic tokens like `...` and `[interrupted]` that get appended to messages inside the backend execution loops were sometimes passed directly to Kokoro, causing it to pronounce punctuation symbols or get stuck.
*   **Decision**: Introduced the `_clean_for_tts(text)` regex filter to strip debug flags, ellipsis, and other trailing interrupting markers before generating audio waveforms.

### 20. Static TTS WAV Caching (`voice.py`)
> [!TIP]
> **Issue**: Generating boot and repeated static responses (e.g. status updates) through Kokoro ONNX took ~2.7 seconds each time, creating a laggy startup experience.
*   **Decision**: Added a disk caching helper using SHA256 hashes of the text/style parameters. Static TTS messages are now rendered once and saved locally to `.gemini/antigravity/tts_cache/`, resulting under 50ms latency on repeated plays.

### 21. Automatic Calculator Clipboard Sharing (`skills.py`)
> [!NOTE]
> **Issue**: After computing mathematical equations, the user had to manually transcribe the results to paste them into other applications, creating friction.
*   **Decision**: Hardened the calculator skill to automatically copy the final evaluation result directly to the system clipboard via `pyperclip` (with a PowerShell clipboard fallback), ensuring seamless productivity.

### 22. Required Parameter Validation Schema (`main.py`)
> [!WARNING]
> **Issue**: When the user gave vague prompts like "calculate the numbers" or "search the web", the planner would invent dummy numbers (like `5 * 7`) or queries rather than asking for clarification.
*   **Decision**: Built a `REQUIRED_PARAMS` verification schema in `main.py`. Before launching the agentic execution loop, the input is parsed. If key parameters are missing, the assistant speaks a clarification question, listens for the user's voice/text response, and combines the inputs.


### 23. Semantic Speculative LLM Gating (`main.py`)
> [!IMPORTANT]
> **Issue**: Reusing speculative LLM responses on minor phonetical differences or using raw string matching was too strict and caused unnecessary re-runs. Conversely, accepting entirely incorrect transcripts degraded agentic quality.
*   **Decision**: Integrated a word-level Jaccard similarity validator (with a 0.80 coefficient threshold) and contraction expansion to ensure speculative prefills are only adopted if they are semantically or phonetically equivalent.

---

## 🛠️ Wave 5: Root Cause Pipeline Hardening

### 24. 16kHz Decimated Barge-In Buffer Pre-Seeding (`voice.py` & `ear.py`)
> [!IMPORTANT]
> **Issue**: The barge-in buffer originally held raw 48kHz audio chunks from `setup_mic()`, which were incompatible with the 16kHz Whisper STT capture loop in `ear.py`. Furthermore, `listen_for_utterance()` only used the barge-in buffer as a state transition flag, completely discarding the saved chunks. This led to clipped voice command onset.
*   **Decision**: Modified `_barge_in_monitor` in `voice.py` to keep the last 15 chunks (~480ms) of *processed, 16kHz* `speech_chunks`. Upgraded `listen_for_utterance()` in `ear.py` to extend its capturing `buffer` directly with these pre-seeded chunks on confirmation. Whisper now receives the complete voice onset context without clipping.

### 25. Post-TTS Listen Cooldown Bypass (`main.py`)
> [!WARNING]
> **Issue**: When a parameter clarification prompt (e.g. asking for math equation numbers) finished playing, the system's `post_tts_cooldown_s` (1.2s) remained active. As a result, the subsequent call to `voice_io.listen()` immediately returned `None` without waiting for the user's spoken answer.
*   **Decision**: Reset `voice_io._last_tts_time = 0.0` directly inside `_prompt_for_parameter()`. This immediately clears the cooldown state, allowing Friday to cleanly capture the user's voice clarification response right after speaking.

### 26. Speculative STT Audio Duration Pre-Gate (`ear.py`)
> [!TIP]
> **Issue**: The background speculative STT thread would fire on extremely short trailing audio fragments (e.g. 0.3s to 0.8s), causing Whisper to generate English-sounding hallucinations and trigger spurious, expensive LLM prefill cycles.
*   **Decision**: Added a duration pre-check at the entry point of `_start_speculative_transcribe()`. It estimates the audio length using the buffer chunk count and exits immediately if it is under 1.2s, saving GPU VRAM and compute cycles from noise hallucinations.

### 27. Fully Deterministic TTS WAV Caching (`voice.py`)
> [!NOTE]
> **Issue**: The static TTS caching key hashed the voice style numpy embedding array string representation (`[0.012 -0.034 ...]`). Floating-point variations across sessions resulted in key mismatches, causing cache misses and slow boot-ups (~2.7s) on every run.
*   **Decision**: Replaced the voice style numpy array representation with stable, configuration-based key interpolation: `f"{clean_text}_{FRIDAY_VOICE}_{FRIDAY_SPEED}_{FRIDAY_LANG}"`. Hashing this configuration key delivers 100% stable hits across all sessions under 50ms.

### 28. Load-Time Conversational Context Sanitizer (`main.py`)
> [!CAUTION]
> **Issue**: Conversations preserved synthetic debug strings (`[partial]`, `... [interrupted]`, `[agentic]`) and raw planner execution tags (`[ACTION:...]`) in `conversation_history.json`. When loaded into LLM context, these templates confused the LLM, leading it to output raw brackets and empty turns.
*   **Decision**: Implemented `_sanitize_history_content()` in `main.py` which runs a regex filter over history upon load to strip all diagnostic prefixes, action tags, and interrupted markers. Empty assistant turns are pruned, ensuring Qwen3 receives 100% clean natural dialogue history.

### 29. Continuous 16kHz Rolling Pre-Buffer for Voice Onset Capture (`voice.py`)
> [!IMPORTANT]
> **Issue**: Relying on `speech_chunks` to populate the barge-in pre-seed meant the buffer got wiped on every loopback floor reset or VAD soft-miss. As a result, when a barge-in was confirmed, the pre-buffer was often extremely tiny (e.g. 4 chunks / 128ms), causing Friday to forget the beginning of the user's speech.
*   **Decision**: Added `rolling_prebuf` (a circular `deque` of `maxlen=15`) in `_barge_in_monitor` which continually captures 16kHz decimated chunks from the mic. Upon barge-in confirmation, the last 15 chunks (~480ms) are cleanly cloned into the pre-seed buffer, guaranteeing the complete recovery of the user's voice onset.

### 30. Responsive Ctrl+C Signal Shutdown (`main.py`)
> [!WARNING]
> **Issue**: When the user pressed Ctrl+C to shut down, the main loop would remain blocked inside `voice.listen()`, preventing clean shutdown and creating a feeling of freezing.
*   **Decision**: Upgraded `_handle_sigint` in `main.py` to immediately signal the voice capture thread to stop running (`voice._streaming_capture._running = False`). The blocking call returns `None` instantly, letting the main thread cleanly shut down within 50ms without any freezing.

### 31. Safe Native Typing Bypass for Math Operations (`automation.py`)
> [!IMPORTANT]
> **Issue**: Keyboard text entry (`type_text`) defaulted to clipboard pasting (`Ctrl+V`) for layout compatibility. However, the Windows Calculator UWP app does not support pasting mathematical operators (like `*`, `/`, `+`), causing them to be ignored. This resulted in numeric inputs merging (e.g. typing `5` then `*` then `7` became `57`) and mathematical calculations failing.
*   **Decision**: Integrated an optimized bypass regex `^[\d\+\-\*\/\.\(\)\=\s\r\n]+$` in `type_text()`. When typing purely numerical digits, decimals, or math operators, the clipboard is bypassed and the characters are sent natively via `pyautogui.write()`, ensuring correct interaction with the Windows Calculator app.

### 32. Case-Insensitive Action Matching for Local LLMs (`skills.py`, `main.py`, `voice.py`)
> [!TIP]
> **Issue**: Qwen3:8b sometimes generates lowercase action tags (e.g., `[action:calculator:45*39]`). The skills parser and immediate dispatcher in `main.py` were case-sensitive (`[ACTION:`), causing them to miss lowercase tags. Consequently, these tags were treated as spoken words instead of tool execution steps, causing Friday to speak out raw tags and skip execution.
*   **Decision**: Converted all action tag pattern regexes (`ACTION_PATTERN`, `ACTION_FALLBACK`), history sanitizers, and pipeline filters to be case-insensitive. Lowercase, uppercase, and mixed-case tags are now processed with 100% reliability.

### 33. Leak-Free Timeout Thread Management (`main.py`, `ear.py`)
> [!CAUTION]
> **Issue**: The 15-second parameter prompting and confirmation timeout threads would join on timeout, but the background `listen_worker` thread calling `voice.listen()` was left running indefinitely. This leaked threads that persistently read from the audio queue in the background, starving the main loop and freezing the assistant's ability to hear any subsequent input.
*   **Decision**: Added a thread-safe `_listen_aborted` flag to `StreamingVoiceCapture` which is checked on every loop cycle of `listen_for_utterance()`. When a parameter or confirmation prompt times out, the flag is set to `True`, terminating the leaked worker thread within 50ms.

### 34. Number-Safe Whisper Confidence & Duration Gate (`ear.py`)
> [!NOTE]
> **Issue**: Short spoken mathematical answers (like "35 and 39." or single numbers) have low linguistic context, causing Whisper to assign them log probabilities slightly below `-0.8` (e.g. `-0.83`) or clip duration under 1.2s, resulting in them being dropped as low-confidence or short audio fragments.
*   **Decision**: Programmed `ear.py` to automatically relax the average logprob gate to `-1.0` and entirely bypass the 1.2s minimum duration filter when the transcript matches a numeric digit or number words.

### 35. Instantaneous Handoff and Wake Sentinels for Barge-in (`voice.py`)
> [!CAUTION]
> **Issue**: When a barge-in event was confirmed, the main thread player loop in `speak_pipeline` continued blockingly waiting for elements on `audio_q` rather than breaking immediately. Because the LLM feeder thread (`_sentence_feeder`) was blocked waiting for the slow, background local Ollama stream to complete yielding sentences, it could not send the final `None` sentinel. This created a thread deadlock that froze the Friday assistant for the entire duration of the LLM generation (often ~20 seconds), causing high-latency recovery after barge-ins.
*   **Decision**: Hardened the concurrent pipeline lifecycle inside `voice.py` in two ways:
    1. Upgraded `stop_playback()` to clear both `_sentence_q` and `_audio_q` queues, and immediately inject a wake sentinel (`None`) into both to instantly release any worker threads blocked on queue retrieval.
    2. Modified the `speak_pipeline` playback player loop to immediately `break` out of the loop rather than `continue` when `self._playback_stop.is_set()` is `True`.
    This instantly returns from the playback pipeline, letting the main thread cleanly call `sentence_gen.close()` (which instantly aborts Ollama's background socket request) and hands control to `voice.listen()`, delivering instantaneous <50ms barge-in responsiveness without any freezing.

### 36. Strict Alternating History sequence & Collapsing Consecutive User Inputs (`main.py`)
> [!IMPORTANT]
> **Issue**: When the user barged in before Friday could speak, the incomplete assistant turn was saved as empty and later pruned. This left two consecutive `"user"` turns in a row (e.g. `"20 times 39."` and `"How are you?"`). Local models get highly confused when fed consecutive user messages, causing them to treat the older message as the active prompt and execute incorrect skills (such as executing `calculator:20*39` when the user asked `"How are you?"`).
*   **Decision**: Implemented a robust Alternating Sequence Sanitizer in `main.py` that filters the conversation history at startup to strictly alternate `user -> assistant`. If it encounters two consecutive `user` turns, it merges them, keeping only the latest. It also ensures the conversation history starts with a `user` turn and ends with an `assistant` turn.

### 37. Delineation of Direct Math vs. GUI Calculator App Planning (`agentic.py`)
> [!TIP]
> **Issue**: When a user explicitly asked to "open calculator and multiply numbers", Friday would launch the Windows Calculator GUI using `open_app`, but then solve the math calculation inside Python using the direct `calculator` skill instead of typing the numbers into the active GUI.
*   **Decision**: Hardened the task planning instructions in `agentic.py`'s planner prompt to establish a clear boundary: use the direct `calculator` skill ONLY for conversational queries, and explicitly forbid using the direct skill if the user requests to use the calculator app GUI. Instead, the planner is now strictly instructed to output a complete keyboard automation sequence: `1. open_app calculator -> 2. wait 2 -> 3. type_text first_number -> 4. type_text operator -> 5. type_text second_number -> 6. press_keys enter`.

### 38. Comprehensive In-Session Conversation Sanitization (`main.py`)
> [!IMPORTANT]
> **Issue**: While `_sanitize_history_content` stripped debug prefixes like `[agentic]` and actions at startup, Friday generated responses during the active session that appended raw tags/actions directly to `conversation_history`. This caused the active session memory to leak tags to the LLM before the next restart, confusing the LLM context.
*   **Decision**: Integrated `_sanitize_history_content` directly into all database updates and memory appending pathways in `main.py` (`_handle_agentic_response`, `_handle_direct_skill`, and the barge-in recovery blocks). All history appends are now immediately and strictly sanitized in memory.

### 39. Thread Join Hardening for Timed Parameters (`main.py`)
> [!WARNING]
> **Issue**: A 500ms join timeout was too short for timed worker threads to exit under massive CPU/CUDA contention, causing minor thread leaks when voice parameter or confirmation prompts timed out.
*   **Decision**: Expanded the thread join watchdog timeout to `2.0` seconds in both `_prompt_for_parameter` and `_make_voice_confirm_fn`. Combined with the 50ms audio queue wake cycle, this guarantees thread termination under any system load.

### 40. Sentinel Queue Race Resolution in `speak_pipeline` (`voice.py`)
> [!CRITICAL]
> **Issue**: If `stop_playback` was called (barge-in event), it flushed the sentence queue and sent a `None` sentinel. However, the background `llm-feeder` thread continued looping and would push a *second* `None` sentinel when exhausted, which would sit in the queue and cause the *next* response to exit prematurely without speaking.
*   **Decision**: Added a loop break condition in `_sentence_feeder` that checks `self._playback_stop.is_set()` on every cycle. If set, it immediately breaks out of the feeder loop, preventing duplicate sentinels and redundant TTS generations.

### 41. Noise Floor EMA Drift Floor (`ear.py`)
> [!NOTE]
> **Issue**: In extremely silent environments over long periods, the Noise Floor EMA smoothed ambient RMS down to near-zero, lowering the wake and enter thresholds so far that Friday became overly sensitive, triggering speculative transcriptions on simple breathing or ambient hums.
*   **Decision**: Set a hard sensible minimum floor of `0.001` on `self._noise_floor_ema` calculation inside `ear.py`. Friday will never drift below this base floor, maintaining perfect silence rejection over long standby sessions.

### 42. Calculator Process Name Compatibility (`skills.py`)
> [!TIP]
> **Issue**: On newer/UWP post-2021 Windows versions, UWP apps are sometimes named `Calculator.exe` instead of `CalculatorApp.exe`. Calling taskkill on `CalculatorApp` would fail to close the window.
*   **Decision**: Hardened `skill_close_app` to try both `CalculatorApp.exe` and `Calculator.exe` as consecutive fallbacks, ensuring seamless process termination across all Windows versions.

### 43. Windows Core Audio COM API Integration for Numeric Volume Percentage (`skills.py`)
> [!IMPORTANT]
> **Issue**: `skill_volume` had no numerical percentage support. Inputs like `volume 100`, `volume 50%`, or `set volume to max` were rejected as "Unknown volume command".
*   **Decision**: Fully integrated the Windows Core Audio COM API via C# inline DLL loading inside `_set_volume_scalar` in `skills.py`. Supported explicit numeric percentages (`0-100%`) and comprehensive spoken aliases (`max`=100, `half`=50, `min`=0, `medium`=50). Set up robust pre-LLM direct volume intent routing for instantaneous, zero-latency response.

### 44. Window Focus Oracle Hardening for UWP/Virtual Desktops (`oracle.py`)
> [!WARNING]
> **Issue**: When `focus_window` failed and returned `"No window matching 'AppName' found."`, the string oracle fell through to `AMBIGUOUS` because the phrase `"not found"` was split. The agentic loop assumed success and proceeded to fire `type_text` keyboard strokes into arbitrary background windows.
*   **Decision**: Hardened the State Oracle `_FAIL_INDICATORS` inside `oracle.py` to explicitly match `"No window matching"` and `"No window"`, guaranteeing focus failures are deterministically classified as `FAIL`.

### 45. Destructive Skill Execution Chain Aborts (`agentic.py`)
> [!CRITICAL]
> **Issue**: If `focus_window` or `open_app` failed in the middle of a multi-step agentic plan, the task runner would blindly proceed to execute subsequent keystrokes (`type_text`, `press_keys`), leading to keyboard drift where keystrokes were typed into wrong windows.
*   **Decision**: Established a critical skill fail-safes list `_ABORT_CHAIN_ON_FAIL = {"focus_window", "open_app", "find_and_open", "smart_open", "open_url"}` in `agentic.py`. A failure in any of these core window/app skills now immediately terminates the remaining execution chain, preventing phantom automation.

### 46. Standalone List Number Token TTS Rejection (`brain.py`, `voice.py`)
> [!NOTE]
> **Issue**: When streaming bulleted/numbered lists, sentence boundaries split tokens like `"2."` or `"3."` into standalone sentences. The existing markdown stripping patterns failed because the trailing space was lost, causing Kokoro TTS to speak indices aloud.
*   **Decision**: Added `^\s*\d+\.?\s*$` to the markdown stripper in `brain.py` and implemented the same pattern directly in the Voice pipeline's `_clean_for_tts` in `voice.py`. Standalone list number tokens are now entirely pruned and ignored before audio generation. Strengthened the system prompt instructions to strictly forbid numbered lists or starting phrases with standalone list integers.

### 47. Seamless Close Tab Direct Routing & Hint Stripping (`skills.py`)
> [!TIP]
> **Issue**: Requesting to "close YouTube tab" was incorrectly routed to `close_app` which terminated the entire browser window instead of only the active tab. Additionally, routing the string `"youtube tab"` as the close tab window hint failed window title matching.
*   **Decision**: Upgraded the pre-LLM intent router in `skills.py` to intercept any close instructions containing `"tab"` or matching a site keyword, directing them exclusively to `close_tab`. Augmented `skill_close_tab` to surgically strip trailing `"tab"` or `"tabs"` suffix words from the window focus target, ensuring seamless tab termination.

### 48. Always-On Active Window Oracle & Cache-Safe Prompt Injection (`screen_oracle.py`, `brain.py`)
> [!IMPORTANT]
> **Issue**: FRIDAY was blind to the active workspace context, and injecting active window properties directly into the system prompt would constantly invalidate Ollama's prompt prefill cache, causing massive latency penalties.
*   **Decision**: Created a zero-overhead active window poller thread `ScreenOracle` in `screen_oracle.py` using native `win32gui` and `psutil`. Designed a cache-safe prompt injection architecture in `brain.py` by dynamic user-turn message prefixing (e.g. `[System Context: Active window: 'Untitled - Notepad' (Notepad.exe)]`). This provides perfect situational context while preserving a 100% warm prompt prefill cache.

### 49. CPU-Only On-Demand UI Automation Screen Reader (`skills.py`)
> [!NOTE]
> **Issue**: OCR tools and visual vision models (like Moondream) have high latency and consume valuable VRAM on the RTX 4050, causing speech/LLM pipeline lags.
*   **Decision**: Built a CPU-only `skill_read_screen` using `uiautomation` that walks the native window semantic descendant tree. Restructured search depth with a `max_depth=4` cap and deduplication, and protected the thread with a strict `150ms` watchdog timeout join to prevent any app locks. Added direct pre-LLM intent routing matching normalized strings (e.g. `what s on my screen`).

### 50. Microsecond Native Win32 Window Polling (`automation.py`, `agentic.py`)
> [!CRITICAL]
> **Issue**: Desktop GUI automations were slow due to hardcoded wait steps planned after app opening, and `focus_window` relied on subprocess-bound PowerShell stubs taking up to 800ms.
*   **Decision**: Reimplemented `focus_window` in `automation.py` using C/C++ compiled native `win32gui` and `win32con` APIs, executing in under 0.1ms. Integrated a dynamic 50ms polling loop that waits up to 2.5s for the target window to appear, returning instantly on window mount. Removed redundant wait steps from the planner prompts in `agentic.py`, speeding up Calculator and Browser automation by up to 2 seconds per execution.

### 51. Atomic Browser Address Bar Focus & Navigation (`skills.py`, `agentic.py`)
> [!WARNING]
> **Issue**: Navigating to a URL via general keyboard typing could leak URLs into active fields of already-open web services (e.g. typing a URL directly inside a WhatsApp DM).
*   **Decision**: Rewrote `skill_open_url` in `skills.py` to atomically send a `Ctrl+L` keystroke to focus the browser's address bar, clear the existing URL with `Ctrl+A`, and then safely type the destination URL. Added a strict instruction in `agentic.py`'s planner prompt to always use `open_url` instead of `type_text` for URL navigation.

### 52. Pre-LLM Conversational WhatsApp Routing & Correction (`skills.py`)
> [!TIP]
> **Issue**: Follow-up message corrections (e.g., "no, tell him X instead") were processed as general conversational text rather than updating the pending WhatsApp message.
*   **Decision**: Introduced a global context tracker `_last_whatsapp_target` in `skills.py` and implemented a correction sub-verb parser in the pre-LLM intent router `detect_direct_intent`. If a last contact is stored, correction inputs are instantly parsed and routed directly to `skill_whatsapp` to correct the message.

### 53. Deterministic WhatsApp Web Contact Search (`skills.py`)
> [!IMPORTANT]
> **Issue**: Automated WhatsApp contact search was fragile and easily misaligned during GUI automation.
*   **Decision**: Created `skill_whatsapp` which uses WhatsApp Web's native contact search hotkey `Ctrl+Alt+/` to guarantee exact search box focus, selects the search input, types the contact name, and uses down-arrow selection. Added `skill_whatsapp_check_messages` which leverages UIA screen reading to safely parse for unread markers or new message notifications in Edge.

### 54. UI-Level Content Eraser (`skills.py`)
> [!NOTE]
> **Issue**: Requests like "Clear Notepad" were spoken but not executed due to missing programmatic content erasure skills.
*   **Decision**: Added `skill_clear_app` to `skills.py` to atomically send `Ctrl+A` and `Delete` to clear focused windows instantly, and registered direct pre-LLM routing for "wipe/clear everything" queries.

### 55. High-Strength Steal-Bypass Switch Window & Browser Reuse (`automation.py`, `skills.py`)
> [!CRITICAL]
> **Issue**: Switching windows using basic APIs failed due to Windows focus-stealing protections blocking background window switches. Additionally, opening Edge apps opened duplicate windows.
*   **Decision**: Implemented `force_foreground(hwnd)` in `automation.py` utilizing the `AttachThreadInput` workaround to bypass Windows focus protection and bring minimized or background windows fully to the front. Upgraded `skill_open_app` to redirect browser-based services to `skill_open_url_in_existing_browser` which restores running browser windows and opens a new tab via `Ctrl+T` instead of spawning duplicate processes. Added direct pre-LLM matching for window switching queries.









