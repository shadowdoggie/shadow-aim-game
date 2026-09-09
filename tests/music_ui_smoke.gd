extends SceneTree
## Generated local WAV only. Run with ./launch.py --headless --audio-driver Dummy --script res://tests/music_ui_smoke.gd

class TestMusic:
	extends "res://game/music_panel.gd"
	func _prepare_track(id: String) -> Dictionary:
		var prepared: Dictionary = host.prepared.duplicate(true)
		if prepared.has("track"): prepared.track.id = id
		await get_tree().create_timer(0.03).timeout
		return prepared

class FakeHost:
	extends Node
	var is_playing := false
	var is_replaying := false
	var screen := ""
	var prepared := {"track":{"id":"test-id","title":"Test song","artist":"Fixture","path":"private-path"},"path":"","format":"wav"}
	var library := {"root":"","status":"ready","total":1,"tracks":[{"id":"test-id","title":"Test song","artist":"Fixture"}]}
	func _clear_page() -> VBoxContainer:
		var box := VBoxContainer.new()
		add_child(box)
		return box
	func _label(parent: Node, text: String, _size=18, _color=Color.WHITE) -> Label:
		var label := Label.new()
		label.text = text
		parent.add_child(label)
		return label
	func _paragraph(parent: Node, text: String, _size=18, _color=Color.WHITE) -> Label:
		return _label(parent,text)
	func _card(parent: Node) -> VBoxContainer:
		var box := VBoxContainer.new()
		parent.add_child(box)
		return box
	func _button(parent: Node, text: String, action: Callable, _primary=false) -> Button:
		var button := Button.new()
		button.text = text
		button.pressed.connect(action)
		parent.add_child(button)
		return button
	func _api(_path: String, _method=HTTPClient.METHOD_GET, _payload={}) -> Dictionary:
		await get_tree().process_frame
		return library

var fixture_dir := (OS.get_environment("TEMP") if OS.has_feature("windows") else "/tmp").path_join("shadow-aim-music-smoke-"+str(Time.get_ticks_usec()))
var fixture_path := fixture_dir+"/song.wav"
var short_fixture_path := fixture_dir+"/short-song.wav"

func _initialize() -> void:
	call_deferred("run")
	create_timer(10.0).timeout.connect(func(): quit(1))

func _finalize() -> void:
	if FileAccess.file_exists(fixture_path): DirAccess.remove_absolute(fixture_path)
	if FileAccess.file_exists(short_fixture_path): DirAccess.remove_absolute(short_fixture_path)
	if DirAccess.dir_exists_absolute(fixture_dir): DirAccess.remove_absolute(fixture_dir)

func run() -> void:
	assert(DisplayServer.get_name() == "headless", "Run this test with --headless --audio-driver Dummy")
	assert(DirAccess.make_dir_recursive_absolute(fixture_dir) == OK)
	var wav := AudioStreamWAV.new()
	wav.format = AudioStreamWAV.FORMAT_16_BITS
	wav.mix_rate = 24000
	wav.stereo = false
	var silence := PackedByteArray()
	silence.resize(24000 * 2 * 8)
	wav.data = silence
	assert(wav.save_to_wav(fixture_path) == OK)
	silence.resize(24000 * 2 / 5)
	wav.data = silence
	assert(wav.save_to_wav(short_fixture_path) == OK)
	wav = null
	var host := FakeHost.new()
	host.prepared.path = fixture_path
	host.library.root = fixture_dir
	root.add_child(host)
	var music := TestMusic.new()
	host.add_child(music)
	music.setup(host)
	assert(not music.is_playing(), "Creating music panel must not start playback")
	music.show_page()
	await process_frame
	await process_frame
	assert(music.tracks.size() == 1)
	var original_master := AudioServer.get_bus_volume_db(0)
	var response: Dictionary = await music.play_track({"id":"test-id","title":"Test song"})
	assert(response.ok and music.is_playing() and music.current_track.id == "test-id")
	assert(response.changed_track and not response.track.has("path"),"Playback acknowledgments must use actual public track metadata")
	assert(not music.get_state().track.has("path"),"Published music state must not include file paths")
	music.set_volume(1.5)
	assert(is_equal_approx(AudioServer.get_bus_volume_db(AudioServer.get_bus_index(music.bus_name)),linear_to_db(1.5)))
	assert(AudioServer.get_bus_volume_db(0) == original_master,"Music volume must not change Master")
	music.set_volume(0)
	assert(AudioServer.is_bus_mute(AudioServer.get_bus_index(music.bus_name)))
	music.set_volume(1)
	assert(not AudioServer.is_bus_mute(AudioServer.get_bus_index(music.bus_name)))
	assert(music.pause_music().ok and music.is_paused() and not music.is_playing())
	assert(music.resume_music().ok and music.is_playing())
	assert(music.stop_music().ok and not music.is_playing())
	assert(not music.resume_music().ok,"Stopped playback cannot be resumed as though paused")
	music.play_track({"id":"test-id"})
	music.stop_music()
	await create_timer(0.10).timeout
	assert(not music.is_playing(),"A late prepare response must not restart stopped music")
	response = await music.play_track({"id":"test-id"},Time.get_unix_time_from_system()+0.005)
	assert(not response.ok and not music.is_playing(),"Expired voice request must not play after preparation")
	response = await music.play_track({"id":"test-id"},Time.get_unix_time_from_system()-1.0)
	assert(not response.ok and not music.is_playing())
	response = await music.next_track()
	assert(not response.ok and not music.is_playing(),"Next must not claim to switch a one-song selection")
	var relaxing := [{"id":"calm-one","title":"Calm one"},{"id":"calm-two","title":"Calm two"}]
	response = await music.play_track(relaxing[0],0.0,relaxing,"Relaxing music")
	assert(response.ok and music.get_state().queue_count == 2 and music.get_state().selection_label == "Relaxing music")
	relaxing.append({"id":"not-calm"})
	music.tracks = [{"id":"unrelated-search-result"}]
	response = await music.next_track()
	assert(response.ok and response.changed_track and response.track.id == "calm-two","Next must stay within the original selected mood")
	response = await music.next_track()
	assert(response.ok and response.track.id == "calm-one","A selected queue must loop instead of switching to the global library")
	music.player.stop()
	music._track_finished()
	await create_timer(0.15).timeout
	assert(music.is_playing() and music.current_track.id == "calm-two","Natural completion must advance within the selected queue")
	music.stop_music()
	await create_timer(0.08).timeout
	assert(not music.is_playing(),"Stop must not trigger automatic queue advancement")
	response = await music.next_track()
	assert(response.ok and response.track.id == "calm-one","Explicit Next after Stop must retain the chosen selection")
	# A new selection preparing when the old song ends must win over auto-next.
	music.play_track({"id":"new-selection"})
	music.player.stop()
	music._track_finished()
	await create_timer(0.15).timeout
	assert(music.current_track.id == "new-selection" and music.play_queue.size() == 1)
	response = await music.next_track()
	assert(not response.ok and music.current_track.id == "new-selection","One-song Next must honestly fail without restarting the song")
	response = await music.play_track({"id":"new-selection"})
	assert(response.ok and not response.changed_track,"Replaying the same song must not be acknowledged as a switch")
	music.player.stop()
	music._track_finished()
	await create_timer(0.15).timeout
	assert(music.is_playing() and music.current_track.id == "new-selection","Natural completion may loop a one-song selection")
	host.prepared = {"error":"Fixture decode failure"}
	response = await music.play_track({"id":"test-id"})
	assert(not response.ok and music.is_playing(),"Failed new song must preserve current music")
	assert(music.play_queue.size() == 1 and music.play_queue[0].id == "new-selection","Failed playback must preserve the prior queue")
	music.stop_music()
	# Let real, short WAVs reach EOF: do not emit finished or invoke the handler.
	host.prepared = {"track":{"id":"short","title":"Short fixture"},"path":short_fixture_path,"format":"wav"}
	var started: Array[String] = []
	var natural_finishes := [0]
	music.playback_changed.connect(func(snapshot: Dictionary):
		if snapshot.playing: started.append(str(snapshot.track.id)))
	music.player.finished.connect(func(): natural_finishes[0] += 1)
	var short_queue := [{"id":"short-calm-one"},{"id":"short-calm-two"}]
	response = await music.play_track(short_queue[0],0.0,short_queue,"Relaxing music")
	assert(response.ok)
	var finish_deadline := Time.get_ticks_msec()+3000
	while started.size() < 4 and Time.get_ticks_msec() < finish_deadline:
		await process_frame
	assert(natural_finishes[0] >= 3,"Actual AudioStreamPlayer.finished must fire for completed WAVs")
	assert(started.slice(0,4) == ["short-calm-one","short-calm-two","short-calm-one","short-calm-two"],"Real WAV completion must advance and loop only the active selected queue")
	music.stop_music()
	var started_before_stop := started.size()
	await create_timer(0.1).timeout
	assert(started.size() == started_before_stop and not music.is_playing(),"Stop must prevent further real-EOF queue playback")
	started.clear()
	natural_finishes[0] = 0
	response = await music.play_track({"id":"short-single"})
	assert(response.ok)
	finish_deadline = Time.get_ticks_msec()+2000
	while started.size() < 2 and Time.get_ticks_msec() < finish_deadline:
		await process_frame
	assert(natural_finishes[0] >= 1 and started.slice(0,2) == ["short-single","short-single"],"Actual WAV completion must also loop a single-song selection")
	music.stop_music()
	var bus_name: String = music.bus_name
	root.remove_child(host)
	host.queue_free()
	await process_frame
	await create_timer(0.1).timeout
	assert(AudioServer.get_bus_index(bus_name) == -1,"Owned music bus must be cleaned up")
	print("MUSIC_UI_SMOKE_PASS: native WAV loading, actual EOF advances and loops selected/single-song queues, search UI, no autoplay, independent volume, selection survives browsing/stop, honest same-song result, cancelled/expired prepare, failure preserves song and selection, cleanup")
	quit()
