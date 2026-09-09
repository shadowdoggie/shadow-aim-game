extends Node
## Local library playback stays independent of the current game page and round.

signal playback_changed(context: Dictionary)

const LIME = Color("c5f878")
const TEXT = Color("edf3e9")
const MUTED = Color("98aaa8")
var host: Node
var player: AudioStreamPlayer
var bus_name := "ShadowAimMusic"
var volume := 1.0
var state := "stopped"
var current_track: Dictionary = {}
var library: Dictionary = {}
var tracks: Array = []
var play_queue: Array = []
var selection_label := ""
var message := ""
var query := ""
var _picker: FileDialog
var _now_playing: Label
var _playback_hint: Label
var _pause_button: Button
var _library_hint: Label
var _track_list: VBoxContainer
var _generation := 0
var _search_generation := 0
var _folder_generation := 0
var _library_busy := false
var _poll_elapsed := 0.0
var _owns_bus := false
var _load_tasks: Array[int] = []
var _state_revision := 0
var _preparing := false

func setup(owner_node: Node) -> void:
	host = owner_node
	var bus := AudioServer.get_bus_index(bus_name)
	if bus < 0:
		AudioServer.add_bus()
		bus = AudioServer.bus_count - 1
		AudioServer.set_bus_name(bus,bus_name)
		_owns_bus = true
	player = AudioStreamPlayer.new()
	player.bus = bus_name
	player.finished.connect(_track_finished)
	add_child(player)
	set_volume(volume)

func show_page() -> void:
	if host.is_playing or host.is_replaying: return
	host.screen = "music"
	_render_page()
	_refresh_library()

func _render_page() -> void:
	if host.screen != "music": return
	var body: VBoxContainer = host._clear_page()
	host._label(body,"YOUR MUSIC",14,LIME)
	host._label(body,"Your soundtrack. Your pace.",40)
	host._paragraph(body,"Choose a music folder, then play a song here or ask Juniper for it. Music keeps playing while you train.",20)
	var controls: VBoxContainer = host._card(body)
	_now_playing = host._paragraph(controls,"",23,TEXT)
	_playback_hint = host._paragraph(controls,"",15)
	var buttons := HBoxContainer.new()
	buttons.add_theme_constant_override("separation",12)
	controls.add_child(buttons)
	_pause_button = host._button(buttons,"Pause",func():
		if is_paused(): resume_music()
		else: pause_music())
	host._button(buttons,"Stop",stop_music)
	host._button(buttons,"Next song",next_track)
	var folder_row := HBoxContainer.new()
	folder_row.add_theme_constant_override("separation",12)
	body.add_child(folder_row)
	host._button(folder_row,"Choose music folder…",_choose_folder,true)
	if not str(library.get("root","")).is_empty(): host._button(folder_row,"Scan folder again",func(): _scan_folder(str(library.root)))
	_library_hint = host._paragraph(body,"",15)
	var search_row := HBoxContainer.new()
	search_row.add_theme_constant_override("separation",12)
	body.add_child(search_row)
	var search := LineEdit.new()
	search.placeholder_text = "Find a song, artist or album…"
	search.text = query
	search.max_length = 200
	search.custom_minimum_size.y = 48
	search.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	search_row.add_child(search)
	search.text_submitted.connect(_search)
	host._button(search_row,"Search",func(): _search(search.text))
	_track_list = VBoxContainer.new()
	_track_list.add_theme_constant_override("separation",12)
	body.add_child(_track_list)
	_update_playback_ui()
	_update_library_ui()
	_render_tracks()

func _choose_folder() -> void:
	if is_instance_valid(_picker):
		_picker.popup_centered_ratio(0.72)
		return
	_picker = FileDialog.new()
	_picker.title = "Choose your music folder"
	_picker.access = FileDialog.ACCESS_FILESYSTEM
	_picker.file_mode = FileDialog.FILE_MODE_OPEN_DIR
	_picker.use_native_dialog = true
	var folder: String = str(library.get("root",""))
	if not folder.is_empty() and DirAccess.dir_exists_absolute(folder): _picker.current_dir = folder
	_picker.dir_selected.connect(_scan_folder)
	add_child(_picker)
	_picker.popup_centered_ratio(0.72)

func _scan_folder(path: String) -> void:
	_folder_generation += 1
	_search_generation += 1
	var generation := _folder_generation
	query = ""
	message = "Scanning your music folder…"
	_update_playback_ui()
	var response: Dictionary = await host._api("/music/folder",HTTPClient.METHOD_POST,{"path":path})
	if generation != _folder_generation: return
	if response.get("error") != null:
		message = "Could not scan this folder: " + str(response.error)
		_update_playback_ui()
		return
	library = response
	tracks = library.get("tracks",[])
	message = ""
	if host.screen == "music": _render_page()

func _refresh_library() -> void:
	if _library_busy: return
	_library_busy = true
	var generation := _folder_generation
	var response: Dictionary = await host._api("/music")
	_library_busy = false
	if generation != _folder_generation: return
	if response.get("error") != null and not response.has("status"):
		message = "Music library unavailable: " + str(response.error)
		_update_playback_ui()
		return
	var previous_root: String = str(library.get("root",""))
	library = response
	if query.is_empty(): tracks = library.get("tracks",[])
	if host.screen == "music" and previous_root != str(library.get("root","")):
		_render_page()
		return
	_update_library_ui()
	if host.screen == "music": _render_tracks()

func _search(text: String) -> void:
	query = text.strip_edges()
	_search_generation += 1
	var generation := _search_generation
	var response: Dictionary = await host._api("/music/search?q="+query.uri_encode())
	if generation != _search_generation: return
	if response.get("error") != null:
		message = "Search unavailable: " + str(response.error)
		_update_playback_ui()
		return
	tracks = response.get("tracks",[])
	_render_tracks()

func _render_tracks() -> void:
	if host.screen != "music" or not is_instance_valid(_track_list): return
	for child in _track_list.get_children():
		_track_list.remove_child(child)
		child.queue_free()
	if tracks.is_empty():
		host._paragraph(_track_list,"No matching songs." if not query.is_empty() else "Choose a folder to find your songs." if str(library.get("root","")).is_empty() else "Your songs will appear here after the scan." if str(library.get("status","")) == "scanning" else "No supported audio files found in this folder.",18)
		return
	for item in tracks:
		if not item is Dictionary: continue
		var track: Dictionary = item
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation",16)
		_track_list.add_child(row)
		var label: Label = host._paragraph(row,_track_title(track),18,TEXT)
		label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		host._button(row,"Play",func(): play_track(track,0.0,tracks,query))
	if query.is_empty() and int(library.get("total",tracks.size())) > tracks.size():
		host._paragraph(_track_list,"Showing the first %d songs. Search to find more." % tracks.size(),14)

func _update_library_ui() -> void:
	if not is_instance_valid(_library_hint): return
	var folder: String = str(library.get("root",""))
	var status: String = str(library.get("status","idle"))
	if status == "scanning":
		_library_hint.text = "Scanning… %d songs found. You can keep training." % int(library.get("total",0))
	elif library.get("error") != null:
		_library_hint.text = "Scan issue: " + str(library.error)
	elif folder.is_empty():
		_library_hint.text = "Music stays on this computer. Nothing plays until you choose a song."
	else:
		_library_hint.text = "%d songs · %s" % [int(library.get("total",0)),folder]

func play_track(track: Dictionary, expires_at: float = 0.0, queue: Array = [], label: String = "") -> Dictionary:
	var id: String = str(track.get("id",""))
	if id.is_empty(): return {"ok":false,"error":"Choose a song from your indexed music folder."}
	if _request_expired(expires_at): return {"ok":false,"error":"The music request expired before playback started."}
	var previous_id: String = str(current_track.get("id",""))
	var next_queue := _make_queue(track,queue)
	_generation += 1
	var generation := _generation
	_preparing = true
	message = "Loading " + _track_title(track) + "…"
	_update_playback_ui()
	var response: Dictionary = await _prepare_track(id)
	if generation != _generation: return {"ok":false,"error":"Playback request was replaced or stopped."}
	if _request_expired(expires_at): return _playback_error("The music request expired before playback started.")
	if response.get("error") != null:
		return _playback_error("Could not load this song: " + str(response.error))
	var path: String = str(response.get("path",""))
	if not path.is_absolute_path() or not FileAccess.file_exists(path):
		return _playback_error("The prepared music file is unavailable. Scan the folder again.")
	var format: String = str(response.get("format",path.get_extension())).to_lower()
	var loaded := {"stream":null}
	# Decode away from rendering/input; playback itself is always changed on the main thread.
	var task := WorkerThreadPool.add_task(func(): loaded.stream = _load_stream(path,format))
	_load_tasks.append(task)
	while not WorkerThreadPool.is_task_completed(task):
		await get_tree().create_timer(0.02).timeout
	WorkerThreadPool.wait_for_task_completion(task)
	_load_tasks.erase(task)
	if generation != _generation: return {"ok":false,"error":"Playback request was replaced or stopped."}
	if _request_expired(expires_at): return _playback_error("The music request expired before playback started.")
	var stream: AudioStream = loaded.stream
	if stream == null: return _playback_error("Godot could not decode this audio file.")
	player.stop()
	player.stream = stream
	player.stream_paused = false
	player.play()
	if not player.playing:
		state = "stopped"
		return _playback_error("This song could not start playing. Please try another song.")
	current_track = response.get("track",track).duplicate(true)
	play_queue = next_queue
	selection_label = label.strip_edges()
	_preparing = false
	state = "playing"
	message = ""
	_publish_state()
	return {"ok":true,"track":get_state().track,"state":state,"changed_track":previous_id != str(current_track.get("id",""))}

func _make_queue(track: Dictionary, selection: Array) -> Array:
	var result: Array = []
	var ids := {}
	for item in selection:
		if not item is Dictionary: continue
		var id: String = str(item.get("id",""))
		if id.is_empty() or ids.has(id): continue
		ids[id] = true
		result.append(item.duplicate(true))
	if not ids.has(str(track.get("id",""))): result.push_front(track.duplicate(true))
	return result

func _request_expired(expires_at: float) -> bool:
	return expires_at > 0.0 and Time.get_unix_time_from_system() >= expires_at

func _prepare_track(track_id: String) -> Dictionary:
	if str(host.token).is_empty(): return {"error":"Launch Shadow Aim to connect your music library."}
	var request := HTTPRequest.new()
	# A selected FLAC/mix can need conversion; ordinary API requests use a shorter timeout.
	request.timeout = 90.0
	add_child(request)
	var headers := PackedStringArray(["Authorization: Bearer "+str(host.token),"Content-Type: application/json"])
	var error := request.request(str(host.base_url)+"/music/prepare",headers,HTTPClient.METHOD_POST,JSON.stringify({"track_id":track_id}))
	if error != OK:
		request.queue_free()
		return {"error":"Could not reach the local music library."}
	var completed: Array = await request.request_completed
	request.queue_free()
	if int(completed[0]) != HTTPRequest.RESULT_SUCCESS: return {"error":"Preparing the song timed out. Please try again."}
	var decoded = JSON.parse_string((completed[3] as PackedByteArray).get_string_from_utf8())
	return decoded if decoded is Dictionary else {"error":"Invalid music library response."}

static func _load_stream(path: String, format: String) -> AudioStream:
	match format:
		"wav": return AudioStreamWAV.load_from_file(path)
		"mp3": return AudioStreamMP3.load_from_file(path)
		"ogg", "vorbis": return AudioStreamOggVorbis.load_from_file(path)
	return null

func pause_music() -> Dictionary:
	if not is_playing(): return {"ok":false,"error":"No song is currently playing."}
	player.stream_paused = true
	state = "paused"
	_publish_state()
	return {"ok":true,"state":state}

func resume_music() -> Dictionary:
	if not is_paused(): return {"ok":false,"error":"There is no paused song to resume."}
	player.stream_paused = false
	state = "playing"
	_publish_state()
	return {"ok":true,"state":state}

func stop_music() -> Dictionary:
	_generation += 1
	_preparing = false
	player.stop()
	player.stream_paused = false
	state = "stopped"
	message = ""
	_publish_state()
	return {"ok":true,"state":state}

func next_track(expires_at: float = 0.0) -> Dictionary:
	return await _advance_queue(expires_at,false)

func _advance_queue(expires_at: float, repeat_single: bool) -> Dictionary:
	if play_queue.is_empty(): return {"ok":false,"error":"Choose a song or a music selection first."}
	if play_queue.size() == 1 and not repeat_single:
		return {"ok":false,"error":"This selection has only one song. Choose another song or a broader selection."}
	var index := -1
	for i in play_queue.size():
		if str(play_queue[i].get("id","")) == str(current_track.get("id","")):
			index = i
			break
	return await play_track(play_queue[(index+1) % play_queue.size()],expires_at,play_queue,selection_label)

func is_playing() -> bool:
	return is_instance_valid(player) and player.playing and not player.stream_paused

func is_paused() -> bool:
	return is_instance_valid(player) and player.stream_paused and player.stream != null

func set_volume(value: float) -> void:
	volume = clampf(value,0.0,2.0)
	var bus := AudioServer.get_bus_index(bus_name)
	if bus >= 0:
		AudioServer.set_bus_mute(bus,volume <= 0.0)
		AudioServer.set_bus_volume_db(bus,linear_to_db(maxf(volume,0.0001)))
	if is_instance_valid(host) and is_instance_valid(player): _publish_state()

func get_state() -> Dictionary:
	var track: Dictionary = {}
	for key in ["id","title","artist","album"]:
		if current_track.has(key): track[key] = str(current_track[key])
	return {"state":state,"track":track,"playing":is_playing(),"paused":is_paused(),"position_s":player.get_playback_position() if is_instance_valid(player) else 0.0,"volume":volume,"revision":_state_revision,"selection_label":selection_label,"queue_count":play_queue.size()}

func _track_title(track: Dictionary) -> String:
	var title: String = str(track.get("title","Song"))
	var artist: String = str(track.get("artist",""))
	return title + " · " + artist if not artist.is_empty() else title

func _playback_error(detail: String) -> Dictionary:
	_preparing = false
	message = detail
	push_warning("Music playback failed: " + detail)
	_publish_state()
	return {"ok":false,"error":detail}

func _publish_state() -> void:
	_state_revision += 1
	_update_playback_ui()
	var snapshot := get_state()
	playback_changed.emit(snapshot)
	host._api("/music/state",HTTPClient.METHOD_POST,snapshot)

func _update_playback_ui() -> void:
	if is_instance_valid(_now_playing):
		_now_playing.text = _track_title(current_track) if not current_track.is_empty() else "Pick something you want to hear."
	if is_instance_valid(_playback_hint):
		_playback_hint.text = message if not message.is_empty() else {"playing":"Playing · continues during practice","paused":"Paused","stopped":"Stopped"}.get(state,state)
		if message.is_empty() and not selection_label.is_empty():
			_playback_hint.text += " · " + selection_label
	if is_instance_valid(_pause_button):
		_pause_button.text = "Resume" if is_paused() else "Pause"
		_pause_button.disabled = not is_playing() and not is_paused()

func _track_finished() -> void:
	state = "stopped"
	_publish_state()
	# An explicit Play may already be preparing the next selection while the
	# old song ends. Let that request finish instead of replacing it.
	if not _preparing and not play_queue.is_empty(): await _advance_queue(0.0,true)

func _process(delta: float) -> void:
	if str(library.get("status","")) != "scanning": return
	_poll_elapsed += delta
	if _poll_elapsed < 1.0: return
	_poll_elapsed = 0.0
	_refresh_library()

func _exit_tree() -> void:
	_generation += 1
	if is_instance_valid(player): player.stop()
	for task in _load_tasks: WorkerThreadPool.wait_for_task_completion(task)
	_load_tasks.clear()
	if _owns_bus:
		var bus := AudioServer.get_bus_index(bus_name)
		if bus >= 0: AudioServer.remove_bus(bus)
