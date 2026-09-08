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

func _initialize() -> void:
	call_deferred("run")
	create_timer(5.0).timeout.connect(func(): quit(1))

func run() -> void:
	Engine.max_fps = 240
	var voice_script = load("res://game/voice_coach.gd")
	assert(voice_script != null, "Voice script must parse")
	var voice = voice_script.new()
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
	# Ordinary bursty delivery stays continuous after a short startup reserve.
	var jitter = VoiceDouble.new()
	host.add_child(jitter)
	jitter.setup(host)
	jitter._speaker = AudioStreamPlayer.new()
	jitter.add_child(jitter._speaker)
	jitter._start_speaker(24000)
	jitter.is_active = true
	jitter.set_process(true)
	jitter._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(0,960)), "sample_rate":24000, "num_channels":1})
	jitter._play_output()
	assert(jitter._playback_waiting and jitter._output_bytes == 960, "The first 20 ms packet must wait for a jitter reserve")
	jitter._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(960,5760)), "sample_rate":24000, "num_channels":1})
	jitter._play_output()
	assert(not jitter._playback_waiting, "Playback starts once 120 ms of audio is ready")
	var delivered := 5760
	var next_delivery_ms := Time.get_ticks_msec()
	for delay_ms in [20, 40, 20, 20, 40, 20, 40, 20, 20, 40, 20, 40]:
		next_delivery_ms += int(delay_ms)
		while Time.get_ticks_msec() < next_delivery_ms:
			await process_frame
		var bytes := int(delay_ms) * 48
		jitter._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(delivered,delivered + bytes)), "sample_rate":24000, "num_channels":1})
		delivered += bytes
	var diagnostics: Dictionary = jitter.get_audio_diagnostics()
	assert(diagnostics.playback_underruns == 0, "Normal 20/40 ms event batches must not underrun after startup")
	assert(diagnostics.output_dropped_frames == 0, "Normal bursts must preserve all speech")
	# After an actual network stall, recover with a reserve instead of fragments.
	await create_timer(0.3).timeout
	assert(jitter._playback_waiting and jitter.get_audio_diagnostics().playback_rebuffers >= 1, "A genuine playback stall must enter rebuffering")
	jitter._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(0,960)), "sample_rate":24000, "num_channels":1})
	jitter._play_output()
	assert(jitter._playback_waiting, "A single late packet must not restart choppy speech")
	jitter._enqueue_output({"audio":Marshalls.raw_to_base64(encoded.slice(960,5760)), "sample_rate":24000, "num_channels":1})
	jitter._play_output()
	assert(not jitter._playback_waiting, "A rebuilt reserve must restart playback")
	assert(jitter._microphone == null, "Synthetic playback must not open a microphone")
	jitter.stop()
	assert(not voice.is_active and voice.state == "off")
	host.queue_free()
	await create_timer(0.2).timeout
	print("VOICE_AUDIO_SMOKE_PASS: phase continuity, ordered uploads, bursty playback without underruns, volume boost, mic off")
	quit()
