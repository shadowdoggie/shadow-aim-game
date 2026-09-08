extends SceneTree
## In-memory API/settings, no microphone or real music. Use --headless --audio-driver Dummy.

class MusicDouble:
	extends "res://game/music_panel.gd"
	var pending_generation := 0
	var starts := 0
	var fake_playing := false

	func play_track(track: Dictionary, expires_at: float = 0.0) -> Dictionary:
		pending_generation += 1
		var current := pending_generation
		starts += 1
		await get_tree().create_timer(0.08).timeout
		if current != pending_generation: return {"ok":false,"error":"Playback was stopped before the song was ready."}
		if expires_at > 0 and Time.get_unix_time_from_system() >= expires_at: return {"ok":false,"error":"The request expired."}
		if track.get("id") == "broken": return {"ok":false,"error":"The selected song could not be decoded."}
		fake_playing = true
		return {"ok":true,"state":"playing"}

	func stop_music() -> Dictionary:
		pending_generation += 1
		fake_playing = false
		return {"ok":true,"state":"stopped"}

class Host:
	extends "res://game/main.gd"
	var acknowledgements: Dictionary = {}
	var audio_states: Array[Dictionary] = []
	var settings_writes := 0
	var saved_settings: Dictionary = {}
	var queued_actions: Array = []
	var last_queue_sequence := 0

	func _load_settings() -> void:
		pass

	func _save_settings() -> void:
		settings_writes += 1
		saved_settings = settings.duplicate(true)
		saved_settings.merge({"voice_volume":voice_volume,"music_volume":music_volume,"game_volume":game_volume})

	func _retry_pending() -> void:
		pass

	func _api(path: String, _method: int = HTTPClient.METHOD_GET, payload: Dictionary = {}) -> Dictionary:
		if path == "/health": return {"status":"ok","connection":"ready"}
		if path == "/voice/context" or path == "/music/state": return {"ok":true}
		if path == "/audio/state":
			audio_states.append(payload.duplicate(true))
			return {"ok":true}
		if path.begins_with("/controls?"):
			await get_tree().process_frame
			var actions := queued_actions.duplicate(true)
			queued_actions.clear()
			return {"actions":actions,"last_seq":last_queue_sequence}
		if path.begins_with("/controls/") and path.ends_with("/ack"):
			acknowledgements[path.get_slice("/",2)] = payload.duplicate(true)
			return {"ok":true}
		return {"error":"No external service is used by the audio-controls smoke test."}

var host: Host
var created_buses: Array[String] = []

func _initialize() -> void:
	call_deferred("run")
	create_timer(10.0).timeout.connect(func(): quit(1))

func _finalize() -> void:
	for name in created_buses:
		var bus := AudioServer.get_bus_index(name)
		if bus >= 0: AudioServer.remove_bus(bus)

func run() -> void:
	assert(DisplayServer.get_name() == "headless","Run with --headless --audio-driver Dummy")
	var master_volume := AudioServer.get_bus_volume_db(0)
	if AudioServer.get_bus_index("ShadowAimGame") < 0: created_buses.append("ShadowAimGame")
	host = Host.new()
	root.add_child(host)
	host.set_process(false)
	await process_frame
	assert(not host.voice.is_active and host.voice._microphone == null)
	var profile := host.settings.duplicate(true)
	var old_music: Node = host.music
	host.remove_child(old_music)
	old_music.free()
	var music := MusicDouble.new()
	host.add_child(music)
	music.setup(host)
	host.music = music
	AudioServer.add_bus()
	AudioServer.set_bus_name(AudioServer.bus_count-1,"ControlsSmokeVoice")
	created_buses.append("ControlsSmokeVoice")
	host.voice._speaker_bus = "ControlsSmokeVoice"
	var ui := VBoxContainer.new()
	host.add_child(ui)
	host._volume_control(ui,["voice","music","game"])
	host._set_audio_volume("voice",150)
	host._set_audio_volume("music",40)
	host._set_audio_volume("game",0)
	assert(is_equal_approx(host.voice.volume,1.5) and is_equal_approx(music.volume,0.4))
	assert(is_equal_approx(db_to_linear(AudioServer.get_bus_volume_db(AudioServer.get_bus_index("ControlsSmokeVoice"))),1.5))
	assert(is_equal_approx(db_to_linear(AudioServer.get_bus_volume_db(AudioServer.get_bus_index(music.bus_name))),0.4))
	assert(AudioServer.is_bus_mute(AudioServer.get_bus_index("ShadowAimGame")))
	assert(AudioServer.get_bus_volume_db(0) == master_volume,"Changing one channel must not change Master")
	for slider in host.volume_sliders:
		assert(slider.value == host._audio_levels()[str(slider.get_meta("channel"))],"All visible sliders must stay synchronized")
	assert(host.settings == profile,"Audio changes must not alter aim settings or benchmark inputs")
	assert(host.saved_settings.voice_volume == 1.5 and host.saved_settings.music_volume == 0.4 and host.saved_settings.game_volume == 0)
	host._set_audio_volume("game",300)
	assert(host.game_volume == 2.0 and not AudioServer.is_bus_mute(AudioServer.get_bus_index("ShadowAimGame")),"Game boost must be bounded and unmute")
	assert(host.voice.volume == 1.5 and music.volume == 0.4)
	var deadline := Time.get_unix_time_from_system()+10
	host.queued_actions = [
		{"id":"slow-play","seq":1,"type":"music","action":"play","track":{"id":"song"},"expires_at":deadline},
		{"id":"stop","seq":2,"type":"music","action":"stop","expires_at":deadline}]
	host.last_queue_sequence = 2
	await host._poll_controls()
	assert(host.acknowledgements.get("stop",{}).get("status") == "completed","Stop must run while preparation is pending")
	assert(not host.acknowledgements.has("slow-play"),"The game must not acknowledge successful playback before it finishes preparing")
	await create_timer(0.12).timeout
	assert(not music.fake_playing and host.acknowledgements["slow-play"].status == "failed","Stopped preparation must fail truthfully without late playback")
	var writes := host.settings_writes
	host.queued_actions = [{"id":"duplicate","seq":2,"type":"volume","channel":"voice","value":0,"expires_at":deadline}]
	await host._poll_controls()
	assert(not host.acknowledgements.has("duplicate") and host.settings_writes == writes,"Previously consumed sequence numbers must not run again")
	await host._apply_control({"id":"expired-volume","type":"volume","channel":"voice","value":0,"expires_at":Time.get_unix_time_from_system()-1})
	assert(host.acknowledgements["expired-volume"].status == "failed" and host.settings_writes == writes and host.voice.volume == 1.5)
	await host._apply_control({"id":"unknown-channel","type":"volume","channel":"all","value":0,"expires_at":deadline})
	assert(host.acknowledgements["unknown-channel"].status == "failed" and host.settings_writes == writes)
	await host._apply_control({"id":"volume","type":"volume","channel":"music","value":175,"expires_at":deadline})
	assert(host.acknowledgements.volume.status == "completed" and host.acknowledgements.volume.value == 175 and music.volume == 1.75)
	await host._apply_control({"id":"broken-song","type":"music","action":"play","track":{"id":"broken"},"expires_at":deadline})
	assert(host.acknowledgements["broken-song"].status == "failed" and "decoded" in str(host.acknowledgements["broken-song"].error))
	await host._apply_control({"id":"late-song","type":"music","action":"play","track":{"id":"song"},"expires_at":Time.get_unix_time_from_system()+0.02})
	assert(host.acknowledgements["late-song"].status == "failed" and not music.fake_playing)
	assert(not host.voice.is_active and host.voice._microphone == null,"Audio settings and native controls must never activate the microphone")
	root.remove_child(host)
	host.queue_free()
	await create_timer(0.15).timeout
	print("AUDIO_CONTROLS_SMOKE_PASS: independent 0-200% channels, persisted values, synchronized UI, no aim-setting pollution, concurrent stop, truthful acknowledgements, duplicate/expired commands, microphone off")
	quit()
