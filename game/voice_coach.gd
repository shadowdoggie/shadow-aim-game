extends Node
## Native, simultaneous microphone capture and Juniper audio playback.
## Microphone audio starts only after an explicit start() and a connected session.
## There is no speech gate, TTS fallback, or pause in capture while Juniper speaks.

signal status_changed(state: String, message: String)

const INPUT_RATE := 24000
const CHUNK_FRAMES := 480
const MAX_INPUT_CHUNKS := 6
const MAX_UPLOAD_CHUNKS := 5
const MAX_OUTPUT_BYTES := INPUT_RATE / 4 * 2
const POLL_SECONDS := 0.02
const PLAYBACK_RESERVE_SECONDS := 0.12

var is_active := false
var state := "off"
var message := "Voice off"
var microphone_level := 0.0
var volume := 1.0
var _host: Node
var _control: HTTPRequest
var _input: HTTPRequest
var _events: HTTPRequest
var _control_action := ""
var _bridge_started := false
var _input_busy := false
var _events_busy := false
var _poll_elapsed := 0.0
var _sequence := 0
var _capture: AudioEffectCapture
var _microphone: AudioStreamPlayer
var _speaker: AudioStreamPlayer
var _playback: AudioStreamGeneratorPlayback
var _generator: AudioStreamGenerator
var _capture_bus := ""
var _speaker_bus := ""
var _source_rate := 48000.0
var _output_rate := INPUT_RATE
var _samples := PackedVector2Array()
var _sample_cursor := 0.0
var _input_chunk := PackedByteArray()
var _input_chunk_frames := 0
var _input_queue: Array[PackedByteArray] = []
var _output_queue: Array[Dictionary] = []
var _output_bytes := 0
var _output_offset := 0
var _network_failures := 0
var _event_failures := 0
var _last_warning_ms := -10000
var _playback_waiting := true
var _last_playback_skips := 0
var _diagnostic_elapsed := 0.0
var _last_capture_ms := -1
var _input_sent_ms := 0
var _events_sent_ms := 0
var _audio_diagnostics := {
	"captured_frames": 0, "capture_backlog_dropped_frames": 0,
	"capture_ring_dropped_frames": 0, "input_dropped_frames": 0,
	"output_dropped_frames": 0, "input_queue_peak_ms": 0,
	"output_queue_peak_ms": 0, "upload_requests": 0,
	"upload_total_ms": 0, "upload_max_ms": 0, "event_requests": 0,
	"event_total_ms": 0, "event_max_ms": 0, "playback_rebuffers": 0,
	"playback_underruns": 0
}


func setup(host: Node) -> void:
	_host = host
	_control = _make_request(90.0, _control_completed)
	_input = _make_request(5.0, _input_completed)
	_events = _make_request(5.0, _events_completed)
	set_process(false)


func _make_request(timeout_s: float, callback: Callable) -> HTTPRequest:
	var request := HTTPRequest.new()
	request.timeout = timeout_s
	# HTTPRequest is asynchronous already; local packets do not need worker threads.
	# Keeping polling on the main loop also makes cancellation deterministic.
	request.use_threads = false
	request.body_size_limit = 2 * 1024 * 1024
	add_child(request)
	request.request_completed.connect(callback)
	return request


func toggle() -> void:
	if state == "connected" or state == "connecting":
		stop()
	else:
		start()


func start() -> void:
	if state == "connected" or state == "connecting":
		return
	if not is_instance_valid(_host) or str(_host.get("token")).is_empty():
		_set_state("error", "Open Shadow Aim with its launcher to connect Juniper.")
		return
	if not bool(ProjectSettings.get_setting("audio/driver/enable_input", false)):
		_set_state("error", "Microphone input is unavailable. Restart the updated game.")
		return
	_cancel_requests()
	_teardown_audio()
	_sequence = 0
	_network_failures = 0
	_event_failures = 0
	_bridge_started = false
	_poll_elapsed = 0.0
	_control_action = "start"
	_set_state("connecting", "Connecting Juniper… microphone off")
	set_process(true)
	if _request(_control, "/voice/start", HTTPClient.METHOD_POST, {}) != OK:
		_fail("Could not connect to the local voice companion.")


func stop() -> void:
	if is_active:
		_log_audio_diagnostics()
	_cancel_requests()
	_teardown_audio()
	_bridge_started = false
	set_process(false)
	_set_state("off", "Voice off")
	if is_instance_valid(_host) and is_instance_valid(_control):
		_control_action = "stop"
		_request(_control, "/voice/stop", HTTPClient.METHOD_POST, {})


func set_volume(value: float) -> void:
	volume = clampf(value, 0.0, 2.0)
	var bus := AudioServer.get_bus_index(_speaker_bus)
	if bus >= 0:
		AudioServer.set_bus_volume_db(bus, linear_to_db(maxf(volume, 0.0001)))


func clear_playback() -> void:
	_output_queue.clear()
	_output_bytes = 0
	_output_offset = 0
	_playback_waiting = true
	if _playback != null:
		# Generator buffers can only be cleared while inactive. Hold the audio lock
		# across this short reset so the mixer never observes a half-reset stream.
		AudioServer.lock()
		_playback.stop()
		_playback.clear_buffer()
		_playback.start()
		AudioServer.unlock()
		_last_playback_skips = _playback.get_skips()


func _request(request: HTTPRequest, path: String, method: int, payload: Dictionary = {}) -> Error:
	var headers := PackedStringArray([
		"Authorization: Bearer " + str(_host.get("token")),
		"Content-Type: application/json"
	])
	return request.request(str(_host.get("base_url")) + path, headers, method,
		JSON.stringify(payload) if method != HTTPClient.METHOD_GET else "")


func _decode_response(result: int, code: int, body: PackedByteArray) -> Dictionary:
	if result != HTTPRequest.RESULT_SUCCESS:
		return {"error": "The local voice connection stopped responding."}
	var decoded = JSON.parse_string(body.get_string_from_utf8())
	if not decoded is Dictionary:
		return {"error": "The voice companion returned an unreadable response."}
	if code < 200 or code >= 300:
		return {"error": str(decoded.get("error", "The voice request failed."))}
	return decoded


func _control_completed(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	if _control_action != "start" or state != "connecting":
		return
	var response := _decode_response(result, code, body)
	if response.get("error") != null and not str(response.error).is_empty():
		_fail(str(response.error))
		return
	_bridge_started = true
	_apply_state(str(response.get("state", "connecting")), str(response.get("message", "")))
	_poll_elapsed = POLL_SECONDS


func _input_completed(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_input_busy = false
	if not is_active:
		return
	_record_http_time("upload", _input_sent_ms)
	var response := _decode_response(result, code, body)
	if response.get("error") != null and not str(response.error).is_empty():
		_network_problem(str(response.error))
	else:
		_network_failures = 0
	_send_input()


func _events_completed(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_events_busy = false
	if not _bridge_started:
		return
	_record_http_time("event", _events_sent_ms)
	var response := _decode_response(result, code, body)
	if response.get("error") != null and not str(response.error).is_empty():
		_network_problem(str(response.error), true)
		return
	_event_failures = 0
	for event in response.get("events", []):
		if not event is Dictionary:
			continue
		var event_sequence := int(event.get("seq", _sequence + 1))
		if event_sequence <= _sequence:
			continue
		_sequence = event_sequence
		_handle_event(event)
		if not _bridge_started:
			return
	_sequence = maxi(_sequence, int(response.get("last_seq", _sequence)))
	if response.has("state"):
		_apply_state(str(response.state), str(response.get("message", response.get("error", ""))))


func _handle_event(event: Dictionary) -> void:
	match str(event.get("type", "")):
		"audio":
			if is_active:
				_enqueue_output(event)
		"interrupt":
			clear_playback()
		"state", "status":
			_apply_state(str(event.get("state", "")), str(event.get("error", event.get("message", ""))))
		"error":
			_fail(str(event.get("error", event.get("message", "Juniper could not continue."))))


func _apply_state(next: String, detail: String = "") -> void:
	match next:
		"connected":
			if not is_active:
				_begin_audio()
		"connecting":
			_set_state("connecting", "Connecting Juniper… microphone off")
		"off", "disconnected", "closed":
			stop()
		"error":
			_fail(detail if not detail.is_empty() else "Juniper could not connect. Try again.")


func _set_state(next: String, detail: String) -> void:
	var changed := state != next or message != detail
	state = next
	message = detail
	if changed:
		status_changed.emit(state, message)


func _begin_audio() -> void:
	for key in _audio_diagnostics:
		_audio_diagnostics[key] = 0
	_diagnostic_elapsed = 0.0
	_last_capture_ms = -1
	_capture_bus = "ShadowAimMic_" + str(get_instance_id())
	_speaker_bus = "ShadowAimJuniper_" + str(get_instance_id())
	AudioServer.add_bus()
	var capture_index := AudioServer.bus_count - 1
	AudioServer.set_bus_name(capture_index, _capture_bus)
	_capture = AudioEffectCapture.new()
	_capture.buffer_length = 0.2
	AudioServer.add_bus_effect(capture_index, _capture)
	# Capture before muting the bus so the player never hears their microphone.
	AudioServer.set_bus_mute(capture_index, true)
	AudioServer.add_bus()
	AudioServer.set_bus_name(AudioServer.bus_count - 1, _speaker_bus)
	set_volume(volume)
	_microphone = AudioStreamPlayer.new()
	_microphone.bus = _capture_bus
	_microphone.stream = AudioStreamMicrophone.new()
	add_child(_microphone)
	_speaker = AudioStreamPlayer.new()
	_speaker.bus = _speaker_bus
	add_child(_speaker)
	_source_rate = AudioServer.get_mix_rate()
	_input_chunk.resize(CHUNK_FRAMES * 2)
	_input_chunk_frames = 0
	_samples.clear()
	_sample_cursor = 0.0
	_start_speaker(INPUT_RATE)
	_microphone.play()
	is_active = true
	_set_state("connected", "Juniper live · microphone on")


func _start_speaker(rate: int) -> void:
	_retain_stopping_stream(_speaker)
	_speaker.stop()
	_generator = AudioStreamGenerator.new()
	_generator.mix_rate = rate
	_generator.buffer_length = 0.12
	_speaker.stream = _generator
	_speaker.play()
	_playback = _speaker.get_stream_playback() as AudioStreamGeneratorPlayback
	_output_rate = rate
	_playback_waiting = true
	_last_playback_skips = _playback.get_skips()


func _process(delta: float) -> void:
	if is_active:
		microphone_level = maxf(0.0, microphone_level - delta * 2.0)
		_capture_input()
		_send_input()
		_play_output()
		_diagnostic_elapsed += delta
		if _diagnostic_elapsed >= 30.0:
			_diagnostic_elapsed = 0.0
			_log_audio_diagnostics()
	if not _bridge_started:
		return
	_poll_elapsed += delta
	if _poll_elapsed >= POLL_SECONDS and not _events_busy:
		_poll_elapsed = 0.0
		_events_busy = true
		_events_sent_ms = Time.get_ticks_msec()
		if _request(_events, "/voice/events?after=" + str(_sequence), HTTPClient.METHOD_GET) != OK:
			_events_busy = false
			_network_problem("Could not receive Juniper's audio.", true)


func _capture_input() -> void:
	var available := _capture.get_frames_available()
	_audio_diagnostics.capture_ring_dropped_frames = _capture.get_discarded_frames()
	if available == 0:
		return
	# A delayed frame must not turn into a long audio backlog or stall the next frame.
	var max_capture := int(_source_rate * 0.06)
	if available > int(_source_rate * 0.16):
		var discarded := available - max_capture
		_capture.get_buffer(discarded)
		_audio_diagnostics.capture_backlog_dropped_frames += discarded
		_audio_diagnostics.input_dropped_frames += _input_queue.size() * CHUNK_FRAMES + _input_chunk_frames
		_input_queue.clear()
		_input_chunk_frames = 0
		_samples.clear()
		_sample_cursor = 0.0
		available = max_capture
		_warn_buffer("Microphone backlog discarded to keep voice live")
	_consume_capture(_capture.get_buffer(mini(available, max_capture)))


func _consume_capture(frames: PackedVector2Array) -> void:
	if not frames.is_empty():
		_last_capture_ms = Time.get_ticks_msec()
		_audio_diagnostics.captured_frames += frames.size()
	_samples.append_array(frames)
	var step := _source_rate / float(INPUT_RATE)
	var peak := 0.0
	# Fractional phase and the boundary sample survive chunks (including 44.1 kHz).
	while _sample_cursor + 1.0 < _samples.size():
		var index := int(_sample_cursor)
		var sample := _samples[index].lerp(_samples[index + 1], _sample_cursor - index)
		var mono := clampf((sample.x + sample.y) * 0.5, -1.0, 1.0)
		peak = maxf(peak, absf(mono))
		_input_chunk.encode_s16(_input_chunk_frames * 2, int(round(mono * 32767.0)))
		_input_chunk_frames += 1
		_sample_cursor += step
		if _input_chunk_frames == CHUNK_FRAMES:
			_input_queue.append(_input_chunk)
			_input_chunk = PackedByteArray()
			_input_chunk.resize(CHUNK_FRAMES * 2)
			_input_chunk_frames = 0
			if _input_queue.size() > MAX_INPUT_CHUNKS:
				_input_queue.pop_front()
				_audio_diagnostics.input_dropped_frames += CHUNK_FRAMES
				_warn_buffer("Slow voice upload: oldest microphone chunk discarded")
	_audio_diagnostics.input_queue_peak_ms = maxi(int(_audio_diagnostics.input_queue_peak_ms), _input_queue.size() * CHUNK_FRAMES * 1000 / INPUT_RATE)
	var consumed := mini(int(_sample_cursor), maxi(0, _samples.size() - 1))
	_samples = _samples.slice(consumed)
	_sample_cursor -= consumed
	microphone_level = maxf(peak, microphone_level)


func _send_input() -> void:
	if not is_active or _input_busy or _input_queue.is_empty():
		return
	var bytes: PackedByteArray = _input_queue.pop_front()
	# Never wait to batch. Combining chunks already waiting lets input catch up
	# when a local HTTP response spans more than one capture chunk/frame.
	for i in mini(_input_queue.size(), MAX_UPLOAD_CHUNKS - 1):
		bytes.append_array(_input_queue.pop_front())
	_input_busy = true
	_input_sent_ms = Time.get_ticks_msec()
	if _request(_input, "/voice/audio", HTTPClient.METHOD_POST, {
		"audio": Marshalls.raw_to_base64(bytes),
		"sample_rate": INPUT_RATE,
		"num_channels": 1
	}) != OK:
		_input_busy = false
		_network_problem("Could not send microphone audio.")


func _enqueue_output(event: Dictionary) -> void:
	var bytes := Marshalls.base64_to_raw(str(event.get("audio", "")))
	var rate := int(event.get("sample_rate", INPUT_RATE))
	var channels := int(event.get("num_channels", 1))
	if channels not in [1, 2] or rate < 8000 or rate > 96000 or bytes.size() % (2 * channels) != 0:
		_fail("Juniper sent an unsupported audio format.")
		return
	if bytes.is_empty():
		return
	if rate != _output_rate:
		clear_playback()
		_start_speaker(rate)
	_output_queue.append({"bytes": bytes, "channels": channels})
	_output_bytes += bytes.size()
	_audio_diagnostics.output_queue_peak_ms = maxi(int(_audio_diagnostics.output_queue_peak_ms), _output_bytes * 1000 / (2 * channels * rate))
	while _output_bytes > MAX_OUTPUT_BYTES and _output_queue.size() > 1:
		var dropped: Dictionary = _output_queue.pop_front()
		_audio_diagnostics.output_dropped_frames += ((dropped.bytes as PackedByteArray).size() - _output_offset) / (2 * int(dropped.channels))
		_output_bytes -= (dropped.bytes as PackedByteArray).size() - _output_offset
		_output_offset = 0
		_warn_buffer("Speaker backlog discarded to keep voice live")
	if _output_bytes > MAX_OUTPUT_BYTES:
		_audio_diagnostics.output_dropped_frames += _output_bytes / (2 * channels)
		clear_playback()
		_warn_buffer("Oversized voice audio discarded")


func _play_output() -> void:
	if _playback == null:
		return
	var skips := _playback.get_skips()
	if skips > _last_playback_skips and not _playback_waiting:
		_audio_diagnostics.playback_underruns += skips - _last_playback_skips
		_audio_diagnostics.playback_rebuffers += 1
		_playback_waiting = true
	_last_playback_skips = skips
	if _output_queue.is_empty():
		return
	var filling_reserve := _playback_waiting
	if _playback_waiting:
		var queued_frames := 0
		for i in _output_queue.size():
			var queued: Dictionary = _output_queue[i]
			queued_frames += ((queued.bytes as PackedByteArray).size() - (_output_offset if i == 0 else 0)) / (2 * int(queued.channels))
		if queued_frames < int(_output_rate * PLAYBACK_RESERVE_SECONDS):
			return
		_playback_waiting = false
	# Prefill the whole reserve before the mixer's next block, then decode at
	# most 60 ms per frame. A reserve absorbs batched RTP delivery;
	# after a stall, refill it before restarting instead of playing fragments.
	var budget := mini(_playback.get_frames_available(), int(_output_rate * (PLAYBACK_RESERVE_SECONDS if filling_reserve else 0.06)))
	while budget > 0 and not _output_queue.is_empty():
		var chunk: Dictionary = _output_queue[0]
		var bytes: PackedByteArray = chunk.bytes
		var channels := int(chunk.channels)
		var count := mini(budget, (bytes.size() - _output_offset) / (2 * channels))
		var output := PackedVector2Array()
		output.resize(count)
		for i in count:
			var left := bytes.decode_s16(_output_offset) / 32768.0
			var right := left if channels == 1 else bytes.decode_s16(_output_offset + 2) / 32768.0
			output[i] = Vector2(left, right)
			_output_offset += 2 * channels
			_output_bytes -= 2 * channels
		_playback.push_buffer(output)
		budget -= count
		if _output_offset == bytes.size():
			_output_queue.pop_front()
			_output_offset = 0


func _network_problem(detail: String, receiving: bool = false) -> void:
	if receiving:
		_event_failures += 1
	else:
		_network_failures += 1
	if _network_failures >= 3 or _event_failures >= 3:
		_fail(detail)


func _fail(detail: String) -> void:
	stop()
	_set_state("error", detail)
	push_warning("Juniper voice: " + detail)


func _warn_buffer(detail: String) -> void:
	var now := Time.get_ticks_msec()
	if now - _last_warning_ms >= 10000:
		_last_warning_ms = now
		print_verbose("Juniper voice: " + detail)


func get_audio_diagnostics() -> Dictionary:
	var result := _audio_diagnostics.duplicate()
	result["input_queued_ms"] = _input_queue.size() * CHUNK_FRAMES * 1000 / INPUT_RATE
	result["captured_rate"] = _source_rate
	result["last_capture_ms_ago"] = Time.get_ticks_msec() - _last_capture_ms if _last_capture_ms >= 0 else -1
	return result


func _record_http_time(kind: String, sent_ms: int) -> void:
	if sent_ms <= 0:
		return
	var elapsed := Time.get_ticks_msec() - sent_ms
	_audio_diagnostics[kind + "_requests"] += 1
	_audio_diagnostics[kind + "_total_ms"] += elapsed
	_audio_diagnostics[kind + "_max_ms"] = maxi(int(_audio_diagnostics[kind + "_max_ms"]), elapsed)


func _log_audio_diagnostics() -> void:
	print_verbose("Juniper audio: " + JSON.stringify(get_audio_diagnostics()))


func _cancel_requests() -> void:
	for request in [_control, _input, _events]:
		if is_instance_valid(request):
			request.cancel_request()
	_input_busy = false
	_events_busy = false
	_control_action = ""


func _teardown_audio() -> void:
	is_active = false
	microphone_level = 0.0
	for player in [_microphone, _speaker]:
		if is_instance_valid(player):
			_retain_stopping_stream(player)
			player.stop()
			player.queue_free()
	_microphone = null
	_speaker = null
	_playback = null
	_generator = null
	_capture = null
	for bus_name in [_capture_bus, _speaker_bus]:
		var index := AudioServer.get_bus_index(bus_name)
		if index > 0:
			AudioServer.remove_bus(index)
	_capture_bus = ""
	_speaker_bus = ""
	_samples.clear()
	_input_queue.clear()
	_output_queue.clear()
	_input_chunk.clear()
	_input_chunk_frames = 0
	_output_bytes = 0
	_output_offset = 0


func _retain_stopping_stream(player: AudioStreamPlayer) -> void:
	if not is_inside_tree() or player.stream == null:
		return
	# Generator playback uses its stream during the mixer's asynchronous stop.
	# Retain both briefly even if this helper is deleted or immediately restarted.
	var retirement := get_tree().create_timer(0.15)
	retirement.set_meta("voice_stream", player.stream)
	if player.has_stream_playback():
		retirement.set_meta("voice_playback", player.get_stream_playback())


func _exit_tree() -> void:
	_cancel_requests()
	_teardown_audio()
