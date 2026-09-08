extends SceneTree
## Synthetic audio only. Run via launch.py --headless --audio-driver Dummy --script tests/voice_audio_smoke.gd

class Host extends Node:
	var token := "test-only"
	var base_url := "http://127.0.0.1:1"

class VoiceDouble extends "res://game/voice_coach.gd":
	var uploads: Array[Dictionary] = []
	func _capture_input() -> void:
		pass
	func _begin_audio() -> void:
		is_active = true
		_set_state("connected", "Juniper test connected")
	func _request(_request_node: HTTPRequest, path: String, _method: int, payload: Dictionary = {}) -> Error:
		if path == "/voice/audio":
			uploads.append(payload.duplicate(true))
		return OK

class FixedClockVoice extends VoiceDouble:
	func _mixer_quantum_seconds() -> float:
		return 0.01

class ManualMixerVoice extends VoiceDouble:
	func _mixer_quantum_seconds() -> float:
		return 4096.0 / AudioServer.get_mix_rate()
	func prepare_output() -> void:
		_generator = AudioStreamGenerator.new()
		_generator.mix_rate = INPUT_RATE
		_generator.buffer_length = PLAYBACK_MAX_RESERVE_SECONDS
		_playback = _generator.instantiate_playback()
		_playback_capacity_frames = _playback.get_frames_available()
		_playback.start()
		_output_rate = INPUT_RATE
		_playback_waiting = true

func _initialize() -> void:
	call_deferred("run")
	create_timer(5.0).timeout.connect(func(): quit(1))

func run() -> void:
	Engine.max_fps = 240
	var voice_script = load("res://game/voice_coach.gd")
	assert(voice_script != null, "Voice script must parse")
	var voice = FixedClockVoice.new()
	var host = Host.new()
	root.add_child(host)
	host.add_child(voice)
	var buses := AudioServer.bus_count
	voice.setup(host)
	assert(not voice.is_active and voice._microphone == null, "Setup must not open a microphone")
	voice._source_rate = 44100.0
	voice._input_chunk.resize(voice.CHUNK_FRAMES * 2)
	var encoded := PackedByteArray()
	for start in range(0, 44101, 137):
		var frames := PackedVector2Array()
		for i in range(start, mini(start + 137, 44101)):
			frames.append(Vector2.ONE * sin(TAU * 440.0 * float(i) / 44100.0) * 0.6)
		voice._consume_capture(frames)
		while not voice._input_queue.is_empty():
			encoded.append_array(voice._input_queue.pop_front())
	encoded.append_array(voice._input_chunk.slice(0, voice._input_chunk_frames * 2))
	assert(absi(encoded.size() / 2 - 24000) <= 1, "Fractional resampling must preserve duration")
	var max_error := 0.0
	for i in range(encoded.size() / 2):
		var expected := sin(TAU * 440.0 * float(i) / 24000.0) * 0.6
		max_error = maxf(max_error, absf(encoded.decode_s16(i * 2) / 32767.0 - expected))
	assert(max_error < 0.001, "Chunk boundaries must preserve the audio waveform")
	voice._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(0,5760)), "sample_rate":24000, "num_channels":1})
	assert(voice._output_bytes == 5760)
	voice._speaker = AudioStreamPlayer.new()
	voice.add_child(voice._speaker)
	voice._start_speaker(24000)
	voice._play_output()
	assert(voice._output_bytes == 0, "Native generator must consume queued PCM")
	voice._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(0,1920)), "sample_rate":24000, "num_channels":1})
	voice._handle_event({"type":"interrupt"})
	assert(voice._output_bytes == 0 and voice._output_queue.is_empty(), "Interrupt clears pending speech")
	assert(AudioServer.bus_count == buses and voice._microphone == null, "Offline audio test must never capture microphone")
	voice.stop()
	var protocol = VoiceDouble.new()
	host.add_child(protocol)
	protocol.setup(host)
	protocol._control_action = "start"
	protocol.state = "connecting"
	protocol._control_completed(HTTPRequest.RESULT_SUCCESS, 200, PackedStringArray(), '{"state":"connected","error":null}'.to_utf8_buffer())
	assert(protocol.state == "connected" and protocol.is_active and protocol._microphone == null, "Nullable error must not reject successful voice start")
	protocol._input_completed(HTTPRequest.RESULT_SUCCESS, 200, PackedStringArray(), '{"error":null}'.to_utf8_buffer())
	protocol._events_completed(HTTPRequest.RESULT_SUCCESS, 200, PackedStringArray(), '{"state":"connected","events":[],"error":null}'.to_utf8_buffer())
	assert(protocol.state == "connected" and protocol._network_failures == 0 and protocol._event_failures == 0, "Nullable event/audio errors remain connected")
	# A delayed HTTP completion must catch up existing captured speech in order.
	for i in 6:
		protocol._input_queue.append(encoded.slice(i * 960, (i + 1) * 960))
	protocol._send_input()
	assert(protocol.uploads.size() == 1 and protocol._input_queue.size() == 1, "Already queued microphone chunks must be batched immediately")
	assert(Marshalls.base64_to_raw(protocol.uploads[0].audio) == encoded.slice(0, 4800), "Batched upload must preserve PCM sequence")
	protocol._input_completed(HTTPRequest.RESULT_SUCCESS, 200, PackedStringArray(), '{"error":null}'.to_utf8_buffer())
	assert(protocol.uploads.size() == 2 and protocol._input_queue.is_empty(), "Next completion must flush the remaining speech")
	assert(Marshalls.base64_to_raw(protocol.uploads[1].audio) == encoded.slice(4800, 5760))
	var master_volume := AudioServer.get_bus_volume_db(0)
	AudioServer.add_bus()
	protocol._speaker_bus = "VoiceSmokeOutput"
	AudioServer.set_bus_name(AudioServer.bus_count - 1, protocol._speaker_bus)
	protocol.set_volume(1.8)
	assert(is_equal_approx(protocol.volume, 1.8), "Voice boost must allow more than 100 percent")
	assert(is_equal_approx(db_to_linear(AudioServer.get_bus_volume_db(AudioServer.get_bus_index(protocol._speaker_bus))), 1.8), "Voice boost must reach the speaker bus")
	assert(is_equal_approx(AudioServer.get_bus_volume_db(0), master_volume), "Voice boost must not amplify all game audio")
	protocol.set_volume(3.0)
	assert(is_equal_approx(protocol.volume, 2.0), "Voice boost must stay bounded")
	protocol.stop()
	# The real native mixer is advanced explicitly so CI scheduling cannot alter
	# the promised 20/40 ms producer. A 4096-frame device block can consume ~93 ms
	# at once: this exact phase reproduced two underruns with the old 120 ms reserve.
	var jitter = ManualMixerVoice.new()
	host.add_child(jitter)
	jitter.setup(host)
	jitter.prepare_output()
	var reserve_frames: int = jitter._playback_reserve_frames()
	jitter._enqueue_output(pcm_packet(480))
	jitter._play_output()
	assert(jitter._playback_waiting and jitter._output_bytes == 960, "A single 20 ms packet must wait for a reserve")
	jitter._enqueue_output(pcm_packet(reserve_frames - 480))
	jitter._play_output()
	assert(not jitter._playback_waiting, "The complete device-aware reserve must start playback")
	assert(jitter.get_audio_diagnostics().playback_reserve_ms >= 120 and jitter.get_audio_diagnostics().playback_reserve_ms <= 240)
	var periods := [20,40,20,20,40,20,40,20,20,40,20,40]
	var next_delivery := 20
	var next_mix := 0.0
	var delivery_index := 0
	for now_ms in range(1000):
		if now_ms >= next_mix:
			jitter._playback.mix_audio(1.0,4096)
			next_mix += 4096.0 * 1000.0 / AudioServer.get_mix_rate()
		if now_ms % 4 == 0:
			while now_ms >= next_delivery:
				jitter._enqueue_output(pcm_packet(int(periods[delivery_index % periods.size()]) * 24))
				delivery_index += 1
				next_delivery += int(periods[delivery_index % periods.size()])
			jitter._play_output()
	var diagnostics: Dictionary = jitter.get_audio_diagnostics()
	assert(diagnostics.playback_underruns == 0, "Exact 20/40 ms delivery must survive whole device mix blocks: " + JSON.stringify(diagnostics))
	assert(diagnostics.output_dropped_frames == 0, "Normal bursts must preserve every speech sample")
	# Actual undersupply must still be detected, then recover without fragments.
	jitter._playback.mix_audio(1.0,int(AudioServer.get_mix_rate() * 0.5))
	jitter._play_output()
	assert(jitter._playback_waiting and jitter.get_audio_diagnostics().playback_rebuffers == 1, "A genuine half-second supply gap must rebuffer")
	jitter._enqueue_output(pcm_packet(480))
	jitter._play_output()
	assert(jitter._playback_waiting, "One late packet must not restart choppy speech")
	jitter._enqueue_output(pcm_packet(reserve_frames - 480))
	jitter._play_output()
	assert(not jitter._playback_waiting, "A complete reserve must restart playback")
	assert(jitter._microphone == null, "Synthetic playback must not open a microphone")
	jitter.stop()
	assert(not voice.is_active and voice.state == "off")
	host.queue_free()
	await create_timer(0.2).timeout
	print("VOICE_AUDIO_SMOKE_PASS: phase continuity, ordered uploads, bursty playback without underruns, volume boost, mic off")
	quit()

func pcm_packet(frames: int) -> Dictionary:
	var bytes := PackedByteArray()
	bytes.resize(frames * 2)
	return {"audio":Marshalls.raw_to_base64(bytes),"sample_rate":24000,"num_channels":1}
