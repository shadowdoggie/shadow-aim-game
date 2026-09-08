extends Node

const Arena = preload("res://game/arena.gd")
const Diagram = preload("res://game/diagram.gd")
const SensitivityPanel = preload("res://game/sensitivity_panel.gd")
const VoiceCoach = preload("res://game/voice_coach.gd")
const PausePanel = preload("res://game/pause_panel.gd")
const MusicPanel = preload("res://game/music_panel.gd")
const AccountPanel = preload("res://game/account_panel.gd")
const INK = Color("0c1115")
const PANEL = Color("151e23")
const LINE = Color("2b383e")
const TEXT = Color("edf3e9")
const MUTED = Color("98aaa8")
const LIME = Color("c5f878")
const DRILLS = ["clicking", "tracking", "switching"]
const TITLES = {"clicking":"Precision clicking", "tracking":"Smooth tracking", "switching":"Target switching"}
var settings := {"sensitivity_deg_per_count":0.07, "fov":103.0, "duration_s":45.0, "target_scale":1.0, "speed_scale":1.0, "fps_limit":240, "crosshair_size":5.0, "fullscreen":false, "dpi":800.0}
var arena: Node3D
var layer: CanvasLayer
var page: Control
var crosshair: Label
var hud: Label
var notice: Label
var job_id := ""
var job_record_id := ""
var poll_elapsed := 0.0
var polling := false
var is_playing := false
var is_replaying := false
var last_record: Dictionary = {}
var last_report: Dictionary = {}
var comparison: Dictionary = {}
var recommendation: Dictionary = {}
var pending_rounds: Array = []
var benchmarks: Dictionary = {}
var benchmark_reports: Dictionary = {}
var guided := false
var stage := ""
var base_url := ""
var token := ""
var status_text := "Practice ready · connecting coach"
var screen := "home"
var coach_box: VBoxContainer
var job_hint: Label
var countdown_generation := 0
var countdown_active := false
var page_generation := 0
var submitting_coach := false
var sensitivity: Node
var voice: Node
var voice_button: Button
var voice_hint: Label
var cycle_id := ""
var recommendation_record_id := ""
var source_coaching_record_id := ""
var active_training_context: Dictionary = {}
var coach_message := ""
var current_drill := ""
var current_round_settings: Dictionary = {}
var voice_volume := 1.0
var music_volume := 0.5
var game_volume := 1.0
var music: Node
var account: Node
var controls_elapsed := 0.0
var controls_polling := false
var controls_after := 0
var audio_revision := 0
var last_settings_save_error := ""
var pending_voice_advice: Dictionary = {}
var wants_voice_advice := false
var volume_sliders: Array[HSlider] = []
var pause_menu: Node
var paused := false
var paused_countdown := false
var live_info: Dictionary = {}
var live_elapsed := 0.0
var sending_live := false

func _ready() -> void:
	_load_settings()
	Engine.max_fps = int(settings.fps_limit)
	if settings.fullscreen: DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_FULLSCREEN)
	base_url = "http://127.0.0.1:" + OS.get_environment("AIMCOACH_PORT")
	token = OS.get_environment("AIMCOACH_TOKEN")
	arena = Arena.new()
	add_child(arena)
	_setup_game_audio()
	arena.round_finished.connect(_round_finished)
	arena.hud_updated.connect(_hud_updated)
	arena.replay_finished.connect(_finish_replay)
	arena.pause_requested.connect(_pause_game)
	layer = CanvasLayer.new()
	add_child(layer)
	var theme := Theme.new()
	theme.default_font_size = 18
	theme.set_color("font_color", "Label", TEXT)
	theme.set_color("font_color", "Button", TEXT)
	theme.set_color("font_hover_color", "Button", LIME)
	theme.set_stylebox("normal", "Button", _box(Color("1d292e"), 10, LINE))
	theme.set_stylebox("hover", "Button", _box(Color("2b3e37"), 10, Color("628051")))
	theme.set_stylebox("pressed", "Button", _box(Color("36492e"), 10, LIME))
	theme.set_stylebox("focus", "Button", _box(Color.TRANSPARENT, 10, LIME))
	page = Control.new()
	page.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	page.theme = theme
	layer.add_child(page)
	crosshair = Label.new()
	crosshair.text = "+"
	crosshair.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	crosshair.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	crosshair.set_anchors_and_offsets_preset(Control.PRESET_CENTER)
	crosshair.position = Vector2(-20,-25)
	crosshair.size = Vector2(40,50)
	crosshair.add_theme_font_size_override("font_size", int(settings.crosshair_size) * 3 + 8)
	crosshair.add_theme_color_override("font_color", TEXT)
	crosshair.add_theme_color_override("font_shadow_color", INK)
	crosshair.add_theme_constant_override("shadow_offset_x", 1)
	crosshair.add_theme_constant_override("shadow_offset_y", 1)
	crosshair.mouse_filter = Control.MOUSE_FILTER_IGNORE
	layer.add_child(crosshair)
	crosshair.hide()
	hud = Label.new()
	hud.position = Vector2(32,24)
	hud.add_theme_color_override("font_color", TEXT)
	hud.add_theme_font_size_override("font_size", 22)
	layer.add_child(hud)
	hud.hide()
	sensitivity = SensitivityPanel.new()
	add_child(sensitivity)
	sensitivity.setup(self)
	voice = VoiceCoach.new()
	add_child(voice)
	voice.setup(self)
	voice.set_volume(voice_volume)
	voice.status_changed.connect(_voice_status_changed)
	music = MusicPanel.new()
	add_child(music)
	music.setup(self)
	music.set_volume(music_volume)
	account = AccountPanel.new()
	add_child(account)
	account.setup(self)
	pause_menu = PausePanel.new()
	add_child(pause_menu)
	pause_menu.setup(self)
	_home()
	_connect_health()

func _box(color: Color, radius: int = 16, border: Color = Color.TRANSPARENT) -> StyleBoxFlat:
	var b := StyleBoxFlat.new()
	b.bg_color = color
	b.set_corner_radius_all(radius)
	b.set_border_width_all(1)
	b.border_color = border
	b.content_margin_left = 22
	b.content_margin_right = 22
	b.content_margin_top = 18
	b.content_margin_bottom = 18
	return b

func _label(parent: Node, text: String, size: int = 18, color: Color = TEXT) -> Label:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", size)
	label.add_theme_color_override("font_color", color)
	parent.add_child(label)
	return label

func _paragraph(parent: Node, text: String, size: int = 18, color: Color = MUTED) -> Label:
	var label := _label(parent, text, size, color)
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	return label

func _button(parent: Node, text: String, action: Callable, primary: bool = false) -> Button:
	var button := Button.new()
	button.text = text
	button.mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	button.custom_minimum_size.y = 50
	button.pressed.connect(action)
	if primary:
		button.add_theme_stylebox_override("normal", _box(LIME, 10))
		button.add_theme_stylebox_override("hover", _box(Color("dafba7"), 10))
		button.add_theme_stylebox_override("pressed", _box(Color("a8d563"), 10))
		button.add_theme_color_override("font_color", INK)
		button.add_theme_color_override("font_hover_color", INK)
		button.add_theme_color_override("font_pressed_color", INK)
	parent.add_child(button)
	return button

func _gap(parent: Node, height: float = 12) -> void:
	var spacer := Control.new()
	spacer.custom_minimum_size.y = height
	parent.add_child(spacer)

func _card(parent: Node, expand: bool = true) -> VBoxContainer:
	var panel := PanelContainer.new()
	panel.add_theme_stylebox_override("panel", _box(PANEL, 18, LINE))
	if expand: panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	parent.add_child(panel)
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 10)
	panel.add_child(box)
	return box

func _clear_page() -> VBoxContainer:
	page_generation += 1
	for child in page.get_children():
		page.remove_child(child)
		child.queue_free()
	page.show()
	var background := ColorRect.new()
	background.color = INK
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	page.add_child(background)
	var scroll := ScrollContainer.new()
	scroll.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	page.add_child(scroll)
	var margin := MarginContainer.new()
	margin.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for edge in ["left","right"]: margin.add_theme_constant_override("margin_"+edge, 48)
	for edge in ["top","bottom"]: margin.add_theme_constant_override("margin_"+edge, 28)
	scroll.add_child(margin)
	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 18)
	margin.add_child(body)
	var nav := HBoxContainer.new()
	nav.add_theme_constant_override("separation", 12)
	body.add_child(nav)
	_label(nav, "◎", 30, LIME)
	_label(nav, "SHADOW AIM", 21)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	nav.add_child(spacer)
	_button(nav, "Train", _home)
	_button(nav, "History", _history)
	_button(nav, "Music", music.show_page)
	_button(nav, "Settings", _settings_page)
	_button(nav, "Quit", func(): get_tree().quit())
	var voice_row := HBoxContainer.new()
	voice_row.add_theme_constant_override("separation", 16)
	body.add_child(voice_row)
	voice_button = _button(voice_row, "Talk with Juniper", _toggle_voice)
	voice_hint = _paragraph(voice_row, "Live voice · talk while you play · headphones recommended", 14)
	_voice_status_changed(voice.state, voice.message)
	_volume_control(voice_row)
	_gap(body, 4)
	return body

func _volume_control(parent: Node, channels: Array = ["voice"]) -> void:
	for channel in channels:
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation",10)
		parent.add_child(row)
		_label(row,str(channel).capitalize(),15,MUTED)
		var slider := HSlider.new()
		slider.min_value = 0
		slider.max_value = 200
		slider.step = 5
		slider.value = _audio_levels()[channel]
		slider.custom_minimum_size.x = 150
		slider.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		slider.tooltip_text = "Saved volume · above 100% boosts quiet audio"
		row.add_child(slider)
		var amount := _label(row,"%d%%" % int(slider.value),15)
		amount.custom_minimum_size.x = 48
		slider.set_meta("value_label",amount)
		slider.set_meta("channel",channel)
		volume_sliders.append(slider)
		slider.value_changed.connect(func(value): _set_audio_volume(str(channel),value))

func _audio_levels() -> Dictionary:
	return {"voice":voice_volume*100.0,"music":music_volume*100.0,"game":game_volume*100.0}

func _set_voice_volume(value: float) -> void:
	_set_audio_volume("voice",value)

func _set_audio_volume(channel: String, value: float) -> void:
	value = clampf(value,0.0,200.0)
	match channel:
		"voice":
			voice_volume = value / 100.0
			voice.set_volume(voice_volume)
		"music":
			music_volume = value / 100.0
			music.set_volume(music_volume)
		"game":
			game_volume = value / 100.0
			var bus := AudioServer.get_bus_index("ShadowAimGame")
			if bus >= 0:
				AudioServer.set_bus_volume_db(bus,linear_to_db(maxf(game_volume,0.0001)))
				AudioServer.set_bus_mute(bus,is_zero_approx(game_volume))
		_: return
	var remaining: Array[HSlider] = []
	for control in volume_sliders:
		if is_instance_valid(control):
			remaining.append(control)
			if str(control.get_meta("channel","voice")) != channel: continue
			control.set_value_no_signal(value)
			var label: Label = control.get_meta("value_label")
			if is_instance_valid(label): label.text = "%d%%" % int(value)
	volume_sliders = remaining
	_save_settings()
	_publish_audio_state()

func _setup_game_audio() -> void:
	var bus := AudioServer.get_bus_index("ShadowAimGame")
	if bus < 0:
		AudioServer.add_bus()
		bus = AudioServer.bus_count - 1
		AudioServer.set_bus_name(bus,"ShadowAimGame")
	arena._hit_sound.bus = "ShadowAimGame"
	AudioServer.set_bus_volume_db(bus,linear_to_db(maxf(game_volume,0.0001)))
	AudioServer.set_bus_mute(bus,is_zero_approx(game_volume))

func _publish_audio_state() -> void:
	if not is_instance_valid(music): return
	audio_revision += 1
	var state := _audio_levels()
	state.revision = audio_revision
	await _api("/audio/state",HTTPClient.METHOD_POST,state)

func _home() -> void:
	if is_playing or is_replaying: return
	if is_instance_valid(sensitivity) and sensitivity.active: sensitivity.cancel()
	screen = "home"
	_voice_context()
	var body := _clear_page()
	var hero := HBoxContainer.new()
	hero.add_theme_constant_override("separation", 48)
	body.add_child(hero)
	var left := VBoxContainer.new()
	left.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	left.size_flags_stretch_ratio = 1.35
	hero.add_child(left)
	_label(left, "LESS GUESSING. BETTER PRACTICE.", 14, LIME)
	_label(left, "Make every\nrep count.", 64)
	_paragraph(left, "Find one thing to improve. Practice it.\nSee what actually changed.", 21)
	_gap(left, 14)
	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation", 12)
	left.add_child(actions)
	_button(actions, "Start guided baseline  →", _start_baseline, true)
	_label(left, "3 short rounds  ·  About 3 minutes", 15, MUTED)
	_button(left, "Find my sensitivity  →", sensitivity.show_intro)
	var right := _card(hero)
	_label(right, "YOUR NEXT STEP", 14, LIME)
	_label(right, "01  Establish your baseline", 25)
	_paragraph(right, "A little clicking, tracking, and switching. Your coach looks for a specific, measurable place to start.")
	_gap(right, 12)
	for item in ["01   Measure your aim", "02   Get one useful adjustment", "03   Practice, then retest"]:
		_label(right, item, 19)
	_gap(right, 12)
	notice = _paragraph(right, status_text, 14, LIME)
	_button(right,"ChatGPT account",account.show_page)
	_paragraph(right, "Selected aim measurements go to Codex for coaching. Replays stay on this computer.", 13)
	_gap(body, 12)
	_label(body, "OR PICK A DRILL", 14, MUTED)
	var drills := GridContainer.new()
	drills.columns = 2
	drills.add_theme_constant_override("h_separation", 16)
	drills.add_theme_constant_override("v_separation", 16)
	body.add_child(drills)
	var descriptions := ["Land the shot. Make smaller corrections.", "Stay with the target. Find a steady rhythm.", "Find the next target. Arrive under control."]
	for i in range(3):
		var drill: String = DRILLS[i]
		var card := _card(drills)
		var diagram := Diagram.new()
		diagram.mode = drill
		card.add_child(diagram)
		_label(card, TITLES[drill], 23)
		_paragraph(card, descriptions[i], 16)
		_button(card, "Practice  ·  %ds" % settings.duration_s, func():
			guided = false
			stage = "free"
			pending_rounds.clear()
			_begin_round(drill, settings.duplicate(true)), false)
	var reactive := _card(drills)
	_label(reactive,"Reactive tracking",23)
	_paragraph(reactive,"Stay on a target that changes direction. React and settle back on it.",16)
	_button(reactive,"Practice  ·  %ds" % settings.duration_s,func():
		guided = false
		stage = "free"
		pending_rounds.clear()
		var config := settings.duplicate(true)
		config.tracking_motion = "reactive"
		_begin_round("tracking",config))
	_label(body, "Native practice. Local history. One clear next step.", 14, MUTED)

func _start_baseline() -> void:
	guided = true
	stage = "baseline"
	cycle_id = _new_cycle_id()
	source_coaching_record_id = ""
	benchmarks.clear()
	benchmark_reports.clear()
	pending_rounds.clear()
	for drill in DRILLS:
		var config: Dictionary = settings.duplicate(true)
		config.duration_s = 45.0
		config.target_scale = 1.0
		config.speed_scale = 1.0
		pending_rounds.append({"drill":drill,"settings":config})
	_next_round()

func _next_round() -> void:
	if pending_rounds.is_empty(): return
	var next: Dictionary = pending_rounds.pop_front()
	_begin_round(next.drill, next.settings)

func _begin_round(drill: String, config: Dictionary) -> void:
	current_drill = drill
	current_round_settings = config.duplicate(true)
	live_info = {}
	live_elapsed = 0.0
	paused = false
	is_playing = true
	countdown_active = true
	if stage in ["baseline", "free", "retest"]: recommendation = {}
	coach_message = ""
	if stage != "sensitivity": config.erase("seed")
	if cycle_id.is_empty() or stage == "free": cycle_id = _new_cycle_id()
	active_training_context = {"kind":"baseline" if stage == "prepractice_baseline" else stage,"cycle_id":cycle_id}
	if stage in ["practice","retest"]:
		if benchmarks.has(drill): active_training_context.baseline_record_id = str(benchmarks[drill].id)
		if not source_coaching_record_id.is_empty(): active_training_context.source_coaching_record_id = source_coaching_record_id
	_voice_context()
	page.hide()
	crosshair.show()
	hud.show()
	countdown_generation += 1
	var generation := countdown_generation
	for n in [3,2,1]:
		var stage_label: String = {"prepractice_baseline":"Baseline","free":"Free practice"}.get(stage,stage.capitalize())
		if stage == "baseline": stage_label = "Baseline %d/3" % (3-pending_rounds.size())
		if stage == "sensitivity": stage_label = sensitivity.progress_text
		hud.text = "%s  ·  %s\n%s\nStarting in %d" % [_drill_title(drill,config), stage_label, "Hold left mouse on the moving target" if drill == "tracking" else "Click the bright target", n]
		await get_tree().create_timer(0.65).timeout
		if generation != countdown_generation: return
	countdown_active = false
	arena.start_round(drill, config)

func _notification(what: int) -> void:
	if what == NOTIFICATION_APPLICATION_FOCUS_OUT and countdown_active: _pause_game()

func _pause_game() -> void:
	if not is_playing or paused: return
	paused = true
	paused_countdown = countdown_active
	if countdown_active:
		countdown_generation += 1
		countdown_active = false
	else: arena.pause_round()
	crosshair.hide()
	hud.hide()
	pause_menu.show_pause()
	_voice_context()

func _resume_from_pause() -> void:
	if not paused: return
	paused = false
	pause_menu.dismiss()
	if paused_countdown:
		paused_countdown = false
		_begin_round(current_drill,current_round_settings.duplicate(true))
	else:
		arena.resume_round()
		crosshair.show()
		hud.show()
		_voice_context()

func _end_paused_round() -> void:
	if not paused: return
	paused = false
	pause_menu.dismiss()
	countdown_generation += 1
	countdown_active = false
	if paused_countdown:
		paused_countdown = false
		if sensitivity.active: sensitivity.cancel()
		is_playing = false
		pending_rounds.clear()
		_home()
	else: arena.stop_round()
	_voice_context()

func _drill_title(drill: String, config: Dictionary = {}) -> String:
	if drill == "tracking" and config.get("tracking_motion","") == "reactive": return "Reactive tracking"
	return str(TITLES.get(drill,"Practice"))

func _hud_updated(info: Dictionary) -> void:
	live_info = info.duplicate(true)
	if paused: return
	if not is_playing and not is_replaying: return
	var tracking := str(info.get("drill", "")) == "tracking"
	var score_text := "%0.1f%% on target" % float(info.get("tracking_pct",0)) if tracking else "%d hits  ·  %0.0f%% accuracy" % [int(info.get("hits",0)),float(info.get("accuracy",0))]
	hud.text = "%s  ·  %0.0fs  ·  %s\n%s  ·  %d FPS" % ["REPLAY" if is_replaying else _drill_title(str(info.get("drill","clicking")),last_record.get("settings",{}) if is_replaying else current_round_settings), float(info.get("remaining_s",0)), score_text, "Escape to return" if is_replaying else "Escape to pause", Engine.get_frames_per_second()]
	if tracking and not is_replaying: hud.text += "\nHold left mouse while tracking"
	if is_replaying: hud.text += "\nGold trail = your recent aim movement"
	if sensitivity.active: hud.text = sensitivity.progress_text + "\n" + hud.text
	if voice.is_active: hud.text += "\nJuniper live · mic on · V to mute"
	elif voice.state == "error": hud.text += "\nVoice disconnected · V to reconnect"

func _round_finished(record: Dictionary) -> void:
	if sensitivity.active:
		await sensitivity.on_round_finished(record)
		return
	is_playing = false
	crosshair.hide()
	hud.hide()
	if active_training_context.get("kind","") in ["baseline","free","practice","retest"]:
		record["training_context"] = active_training_context.duplicate(true)
	last_record = record
	last_report = {}
	comparison = {}
	if not record.get("completed",false):
		pending_rounds.clear()
		_home()
		return
	if stage in ["baseline","prepractice_baseline"]: benchmarks[record.drill] = record.duplicate(true)
	_results("Saving your round…")
	var response: Dictionary = await _api("/sessions", HTTPClient.METHOD_POST, record)
	if str(last_record.get("id","")) != str(record.id): return
	if response.has("report"):
		last_report = response.report
		comparison = response.get("comparison",{}) if response.get("comparison") is Dictionary else {}
		if stage in ["baseline","prepractice_baseline"]: benchmark_reports[record.drill] = last_report.duplicate(true)
		if screen == "results" and not is_playing and not is_replaying:
			_results()
			_voice_context()
			if pending_rounds.is_empty() and stage not in ["practice","prepractice_baseline"]: _request_coaching()
	else:
		push_warning("Round save failed (%s): %s" % [str(record.id),str(response.get("error","Unknown local companion error"))])
		DirAccess.make_dir_recursive_absolute("user://pending")
		var f := FileAccess.open("user://pending/" + str(record.id).validate_filename() + ".json", FileAccess.WRITE)
		if f: f.store_string(JSON.stringify(record))
		if screen == "results" and not is_playing and not is_replaying:
			_results("Could not save to the companion: " + str(response.get("error","Connection failed.")) + " Your recording is kept locally for retry.")

func _results(message: String = "") -> void:
	screen = "results"
	var body := _clear_page()
	_label(body, "ROUND COMPLETE  /  " + _drill_title(str(last_record.get("drill", "")),last_record.get("settings",{})).to_upper(), 14, LIME)
	_label(body, "Baseline %d of 3 complete." % (3-pending_rounds.size()) if stage == "baseline" else "Your round, measured.", 46)
	if not message.is_empty(): _paragraph(body,message,18,LIME)
	var stats := HBoxContainer.new()
	stats.add_theme_constant_override("separation",14)
	body.add_child(stats)
	if not last_report.is_empty():
		var metrics: Dictionary = last_report.get("metrics",{})
		var count := 0
		var priority: Array = ["accuracy_pct","acquisition_ms","overshoot_pct","hit_interval_ms","correction_ms"]
		if last_record.get("drill") == "tracking": priority = ["time_on_target_pct","tracking_error_deg","frame_p95_ms"]
		var names := {"accuracy_pct":"Accuracy","acquisition_ms":"Target acquisition","overshoot_pct":"Overshoot rate","hit_interval_ms":"Between hits","correction_ms":"Correction time","time_on_target_pct":"Time on target","tracking_error_deg":"Tracking error","frame_p95_ms":"Frame time · p95"}
		for key in priority:
			var metric: Dictionary = metrics.get(key,{})
			if metric.get("value") == null or count >= 4: continue
			var card := _card(stats)
			_label(card,names.get(key,key),14,MUTED)
			_label(card,_metric_text(metric),30)
			card.tooltip_text = str(metric.get("description",""))
			var change: Dictionary = comparison.get("metrics",{}).get(key,{})
			if not change.is_empty():
				_label(card,("Baseline: %0.1f %s" if comparison.get("kind","") == "baseline" else "Previous: %0.1f %s") % [float(change.previous),str(metric.get("unit",""))],14,MUTED)
			count += 1
		var flags: Array = last_report.get("quality",{}).get("flags",[])
		if not flags.is_empty():
			_paragraph(body,"Measurement notes: " + ", ".join(flags).replace("_"," ") + ". Treat this round as a tentative measurement.",14,Color("edc586"))
		if not comparison.is_empty(): _paragraph(body,"Compared with your practice baseline." if comparison.get("kind","") == "baseline" else "Compared with your last round at these settings.",14)
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation",20)
	body.add_child(row)
	coach_box = _card(row)
	coach_box.size_flags_stretch_ratio = 1.3
	_label(coach_box,"NEXT STEP" if not pending_rounds.is_empty() else "YOUR COACH",14,LIME)
	if last_report.is_empty():
		_paragraph(coach_box,"Saving your measurements before the next step…")
	elif not pending_rounds.is_empty():
		_label(coach_box,"Next: " + _drill_title(str(pending_rounds[0].drill),pending_rounds[0].get("settings",{})),25)
		_paragraph(coach_box,"Coaching starts after round 3. Each round measures a different part of your aim." if stage == "baseline" else "Repeat the same cue once more before retesting.")
		_button(coach_box,"Next round  →",_next_round,true)
	elif stage == "practice":
		_label(coach_box,"Now check what changed.",25)
		_paragraph(coach_box,"Retest the original difficulty with a fresh target sequence.")
		_button(coach_box,"Retest baseline  →",_retest,true)
	elif not recommendation.is_empty() and job_id.is_empty():
		_render_coach(coach_box)
	else:
		var waiting := not job_id.is_empty() or submitting_coach
		job_hint = _paragraph(coach_box,coach_message if not coach_message.is_empty() else ("Your coach is reviewing your rounds. Juniper will tell you when it is ready." if waiting and voice.is_active else "Your coach is reviewing your rounds…" if waiting else "Your measurements are ready for review."))
		if waiting:
			if not job_id.is_empty(): _button(coach_box,"Cancel review",_cancel_coaching)
		else: _button(coach_box,"Request coaching",_request_coaching)
	var replay := _card(row)
	_label(replay,"LOOK AT THE EVIDENCE",14,MUTED)
	_label(replay,"Your aim, replayed.",25)
	var chart := Diagram.new()
	chart.samples = last_record.get("samples",[])
	chart.custom_minimum_size.y = 100
	replay.add_child(chart)
	_paragraph(replay,"Horizontal aim error over time. The center line is the target.",14)
	_button(replay,"Watch 3D replay  ↗",_replay)
	if not last_report.is_empty():
		for evidence in last_report.get("evidence",[]).slice(0,2):
			_paragraph(replay,str(evidence.get("summary","")),14)
	_gap(body,6)
	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation",12)
	body.add_child(actions)
	_button(actions,"Back to training",_home)
	_button(actions,"Repeat this drill",func():
		guided = false
		stage = "free"
		pending_rounds.clear()
		_begin_round(str(last_record.drill),last_record.settings.duplicate(true)))

func _metric_text(metric: Dictionary) -> String:
	var value: float = float(metric.get("value",0))
	var unit: String = str(metric.get("unit",""))
	return "%0.1f%s" % [value, " " + unit if not unit.is_empty() else ""]

func _render_coach(parent: VBoxContainer) -> void:
	_label(parent,"Juniper has your review.",25)
	_button(parent,"Hear coaching",func(): _hear_coaching({"coaching":recommendation}),true)
	var drill: String = str(recommendation.get("drill",last_record.get("drill","clicking")))
	_label(parent,"Next: " + _drill_title(drill,benchmarks.get(drill,{}).get("settings",last_record.get("settings",{}))),19)
	_button(parent,"Start coached practice  →",_practice_prescription)
	var cited_rounds: Array = []
	for evidence_id in recommendation.get("evidence_ids",[]):
		var record_id: String = str(evidence_id).get_slice(":",0)
		if record_id not in cited_rounds:
			cited_rounds.append(record_id)
			_button(parent,"Replay supporting round" if cited_rounds.size() == 1 else "Replay supporting round %d" % cited_rounds.size(),func(): _replay_record_id(record_id))

func _hear_coaching(extra: Dictionary) -> void:
	if voice.is_active:
		_voice_context(true,extra)
		return
	pending_voice_advice = extra.duplicate(true)
	wants_voice_advice = true
	await _voice_context(false,extra)
	if wants_voice_advice and voice.state != "connecting": voice.start()

func _practice_prescription() -> void:
	stage = "practice"
	cycle_id = _new_cycle_id()
	source_coaching_record_id = recommendation_record_id
	var drill: String = recommendation.get("drill",last_record.get("drill","clicking"))
	if last_record.get("drill") == drill:
		benchmarks[drill] = last_record.duplicate(true)
		benchmark_reports[drill] = last_report.duplicate(true)
	if not benchmarks.has(drill):
		stage = "prepractice_baseline"
		var baseline_config: Dictionary = settings.duplicate(true)
		baseline_config.duration_s = 45.0
		baseline_config.target_scale = 1.0
		baseline_config.speed_scale = 1.0
		_begin_round(drill,baseline_config)
		return
	var config: Dictionary = benchmarks[drill].settings.duplicate(true)
	config.merge(recommendation.get("parameters",{}),true)
	pending_rounds.clear()
	for i in range(2): pending_rounds.append({"drill":drill,"settings":config.duplicate(true)})
	_next_round()

func _retest() -> void:
	stage = "retest"
	var drill: String = recommendation.get("drill",last_record.get("drill","clicking"))
	var config: Dictionary = settings.duplicate(true)
	if benchmarks.has(drill): config = benchmarks[drill].settings.duplicate(true)
	config.erase("seed")
	_begin_round(drill,config)

func _replay() -> void:
	if last_record.is_empty(): return
	is_replaying = true
	page.hide()
	hud.show()
	crosshair.show()
	arena.start_replay(last_record)

func _replay_record_id(id: String) -> void:
	var generation := page_generation
	var response := await _api("/sessions/"+id.uri_encode())
	if generation != page_generation or is_playing or is_replaying: return
	if not response.has("record"): return
	is_replaying = true
	page.hide()
	hud.show()
	crosshair.show()
	arena.start_replay(response.record)

func _finish_replay() -> void:
	if not is_replaying: return
	is_replaying = false
	crosshair.hide()
	hud.hide()
	_results()

func _input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_V and is_playing:
		_toggle_voice()

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_ESCAPE:
		if is_replaying:
			arena.stop_replay()
			_finish_replay()
		elif paused: _resume_from_pause()
		elif is_playing: _pause_game()
		get_viewport().set_input_as_handled()

func _history() -> void:
	if is_playing or is_replaying: return
	if sensitivity.active: sensitivity.cancel()
	screen = "history"
	var body := _clear_page()
	_label(body,"YOUR PRACTICE",14,LIME)
	_label(body,"Progress starts with showing up.",38)
	_button(body,"Saved sensitivity comparisons",sensitivity.show_intro)
	var loading := _paragraph(body,"Loading saved rounds…")
	var response: Dictionary = await _api("/sessions")
	if screen != "history" or not is_instance_valid(loading): return
	loading.queue_free()
	var sessions: Array = response.get("sessions",[])
	if sessions.is_empty(): _paragraph(body,"No saved rounds yet. Start with a short baseline.")
	for session in sessions:
		var id: String = str(session.get("record_id",session.get("id","")))
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation",16)
		body.add_child(row)
		var description := _label(row,_drill_title(str(session.get("drill","")),{"tracking_motion":session.get("tracking_motion","")}) + "  ·  " + Time.get_datetime_string_from_unix_time(int(session.get("started_at",0))).replace("T"," "),18)
		description.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		_button(row,"Review",func(): _load_session(id))

func _load_session(id: String) -> void:
	var generation := page_generation
	var response: Dictionary = await _api("/sessions/" + id.uri_encode())
	if not response.has("record") or generation != page_generation: return
	last_record = response.record
	last_report = response.get("report",{})
	comparison = response.get("comparison",{}) if response.get("comparison") is Dictionary else {}
	recommendation = response.get("coaching",{}) if response.get("coaching") is Dictionary else {}
	pending_rounds.clear()
	stage = "history"
	recommendation_record_id = id if not recommendation.is_empty() else ""
	_results()
	_voice_context(not recommendation.is_empty(),{"coaching":recommendation})

func _settings_page() -> void:
	if is_playing or is_replaying: return
	if sensitivity.active: sensitivity.cancel()
	screen = "settings"
	var body := _clear_page()
	_label(body,"MAKE IT FEEL RIGHT",14,LIME)
	_label(body,"Your setup.",46)
	_paragraph(body,"Keep these consistent when comparing results. Mouse movement is unsmoothed.")
	_button(body,"ChatGPT account",account.show_page)
	_button(body,"Find my sensitivity  →",sensitivity.show_intro,true)
	var audio_card := _card(body)
	_label(audio_card,"Audio",23)
	_volume_control(audio_card,["voice","music","game"])
	var grid := GridContainer.new()
	grid.columns = 2
	grid.add_theme_constant_override("h_separation",32)
	grid.add_theme_constant_override("v_separation",18)
	body.add_child(grid)
	var calibration := Label.new()
	calibration.add_theme_color_override("font_color",LIME)
	calibration.text = "Current turn distance: %.1f cm / 360°" % (914.4 / (float(settings.dpi) * float(settings.sensitivity_deg_per_count)))
	var fields := [
		["Mouse sensitivity · degrees per count","sensitivity_deg_per_count",0.001,0.5,0.001],
		["Mouse DPI · for cm/360 calculation","dpi",100.0,32000.0,50.0],
		["Horizontal field of view","fov",60.0,130.0,1.0],
		["Free practice round · seconds","duration_s",30.0,90.0,5.0],
		["Target size · free practice","target_scale",0.5,2.0,0.1],
		["Tracking speed · free practice","speed_scale",0.4,2.0,0.1],
		["Frame limit · 0 is uncapped","fps_limit",0.0,1000.0,10.0],
		["Crosshair size","crosshair_size",1.0,12.0,1.0]]
	for field in fields:
		_label(grid,field[0],18)
		var spin := SpinBox.new()
		spin.min_value = field[2]
		spin.max_value = field[3]
		spin.step = field[4]
		spin.value = float(settings[field[1]])
		spin.custom_minimum_size.x = 210
		grid.add_child(spin)
		spin.value_changed.connect(func(value):
			settings[field[1]] = value
			calibration.text = "Current turn distance: %.1f cm / 360°" % (914.4 / (float(settings.dpi) * float(settings.sensitivity_deg_per_count)))
			_save_settings())
	_label(grid,"Fullscreen",18)
	var full := CheckButton.new()
	full.button_pressed = bool(settings.fullscreen)
	grid.add_child(full)
	full.toggled.connect(func(enabled):
		settings.fullscreen = enabled
		DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_FULLSCREEN if enabled else DisplayServer.WINDOW_MODE_WINDOWED)
		_save_settings())
	body.add_child(calibration)
	_paragraph(body,"Calibration: cm/360 = 914.4 ÷ (DPI × sensitivity). Set DPI to your mouse's actual value, then match your preferred turn distance. The app cannot read hardware DPI.",16)
	_button(body,"Save and return",func():
		Engine.max_fps = int(settings.fps_limit)
		crosshair.add_theme_font_size_override("font_size",int(settings.crosshair_size)*3+8)
		_save_settings()
		_home(),true)

func _load_settings() -> void:
	if FileAccess.file_exists("user://settings.json"):
		var parsed = JSON.parse_string(FileAccess.get_file_as_string("user://settings.json"))
		if parsed is Dictionary:
			voice_volume = clampf(float(parsed.get("voice_volume",1.0)),0.0,2.0)
			music_volume = clampf(float(parsed.get("music_volume",0.5)),0.0,2.0)
			game_volume = clampf(float(parsed.get("game_volume",1.0)),0.0,2.0)
			for key in ["voice_volume","music_volume","game_volume"]: parsed.erase(key)
			settings.merge(parsed,true)

func _save_settings() -> void:
	last_settings_save_error = ""
	var file := FileAccess.open("user://settings.json.tmp",FileAccess.WRITE)
	if file:
		var stored := settings.duplicate(true)
		stored.voice_volume = voice_volume
		stored.music_volume = music_volume
		stored.game_volume = game_volume
		file.store_string(JSON.stringify(stored))
		file.flush()
		var error := file.get_error()
		file.close()
		if error != OK or DirAccess.rename_absolute("user://settings.json.tmp","user://settings.json") != OK:
			last_settings_save_error = "Settings applied, but saving failed. Check free space and folder permissions."
	else:
		last_settings_save_error = "Settings applied, but the settings file could not be written."
	if not last_settings_save_error.is_empty():
		push_warning(last_settings_save_error)
		status_text = last_settings_save_error
		if is_instance_valid(notice): notice.text = status_text

func _connect_health() -> void:
	var health := await _api("/health")
	var auth_state: String = str(health.get("account",{}).get("state","checking"))
	status_text = "Practice ready · Codex is connecting"
	if health.get("status") != "ok": status_text = "Offline practice ready · launch Shadow Aim to connect"
	elif auth_state in ["signed_out","pending","error"]:
		status_text = "Practice ready · open ChatGPT account to connect your coach"
		if auth_state == "error": status_text = "Practice ready · " + str(health.get("account",{}).get("error","Check your ChatGPT account."))
	elif health.get("connection") == "ready": status_text = "ChatGPT subscription · " + str(health.get("model","Coach")) + " / " + str(health.get("effort","high"))
	elif health.get("error") != null: status_text = "Coach unavailable · " + str(health.error)
	if is_instance_valid(notice): notice.text = status_text
	if health.get("status") == "ok" and health.get("connection") != "ready" and health.get("error") == null and auth_state not in ["signed_out","pending","error"]:
		await get_tree().create_timer(2).timeout
		_connect_health()
	elif health.get("status") == "ok":
		_retry_pending()
		_publish_audio_state()

func _retry_pending() -> void:
	var dir := DirAccess.open("user://pending")
	if dir == null: return
	for file in dir.get_files():
		if not file.ends_with(".json"): continue
		var record = JSON.parse_string(FileAccess.get_file_as_string("user://pending/"+file))
		if not record is Dictionary: continue
		var response := await _api("/sessions",HTTPClient.METHOD_POST,record)
		if response.has("report"): dir.remove(file)

func _api(path: String, method: int = HTTPClient.METHOD_GET, payload: Dictionary = {}) -> Dictionary:
	if token.is_empty(): return {"error":"Launch with the Aim Coach launcher to enable coaching."}
	var request := HTTPRequest.new()
	request.timeout = 20.0
	add_child(request)
	var headers := PackedStringArray(["Authorization: Bearer " + token,"Content-Type: application/json"])
	var error := request.request(base_url+path,headers,method,JSON.stringify(payload) if method != HTTPClient.METHOD_GET else "")
	if error != OK:
		request.queue_free()
		return {"error":"Could not reach local companion."}
	var completed: Array = await request.request_completed
	request.queue_free()
	if int(completed[0]) != HTTPRequest.RESULT_SUCCESS: return {"error":"Local companion did not respond."}
	var decoded = JSON.parse_string((completed[3] as PackedByteArray).get_string_from_utf8())
	return decoded if decoded is Dictionary else {"error":"Invalid companion response."}

func _request_coaching(question: String = "") -> void:
	if not job_id.is_empty() or last_report.is_empty() or submitting_coach: return
	submitting_coach = true
	coach_message = ""
	recommendation.clear()
	if screen == "results": _results()
	job_record_id = str(last_record.id)
	var response: Dictionary = await _api("/coach",HTTPClient.METHOD_POST,{"record_id":job_record_id,"question":question})
	submitting_coach = false
	if response.has("job_id"):
		job_id = str(response.job_id)
		poll_elapsed = 2.0
		if screen == "results" and not is_playing and not is_replaying: _results()
	elif screen == "results":
		coach_message = "Coach unavailable: " + str(response.get("error","Please try again."))
		_results()

func _cancel_coaching() -> void:
	if job_id.is_empty(): return
	await _api("/jobs/" + job_id + "/cancel",HTTPClient.METHOD_POST)
	job_id = ""
	coach_message = "Review cancelled. Your round is saved."
	if screen == "results" and not is_playing: _results()

func _process(delta: float) -> void:
	controls_elapsed += delta
	if controls_elapsed >= 0.1 and not controls_polling and not token.is_empty():
		controls_elapsed = 0.0
		_poll_controls()
	if is_instance_valid(voice) and voice.is_active and is_playing and not paused and not countdown_active and not live_info.is_empty():
		live_elapsed += delta
		if live_elapsed >= 5.0 and not sending_live:
			live_elapsed = 0.0
			_send_live_measurements()
	if job_id.is_empty() or polling: return
	poll_elapsed += delta
	if poll_elapsed < 2.0: return
	poll_elapsed = 0
	poll_job()

func poll_job() -> void:
	polling = true
	var current := job_id
	var response := await _api("/jobs/" + current)
	polling = false
	if current != job_id: return
	var status: String = str(response.get("status",""))
	if status == "complete":
		job_id = ""
		if str(last_record.get("id","")) == job_record_id:
			recommendation = response.get("result",{})
			recommendation_record_id = job_record_id
			if screen == "results" and not is_playing and not is_replaying: _results()
			_voice_context(true,{"coaching":recommendation})
	elif status in ["failed","cancelled"] or response.get("error") != null:
		job_id = ""
		coach_message = "Coach " + status + ": " + str(response.get("error","Please retry when ready."))
		if screen == "results" and not is_playing and not is_replaying: _results()

func _new_cycle_id() -> String:
	return "%x-%x" % [int(Time.get_unix_time_from_system()),Time.get_ticks_usec()]

func _toggle_voice() -> void:
	if voice.state in ["connected","connecting"]:
		wants_voice_advice = false
		pending_voice_advice = {}
		voice.stop()
		return
	_voice_context()
	voice.start()

func _voice_status_changed(state_name: String, detail: String) -> void:
	if state_name == "connected" and wants_voice_advice:
		wants_voice_advice = false
		var advice := pending_voice_advice
		pending_voice_advice = {}
		_voice_context(true,advice)
	elif state_name == "error": wants_voice_advice = false
	if is_instance_valid(voice_button):
		voice_button.text = "Stop Juniper · mic on" if state_name == "connected" else "Cancel voice" if state_name == "connecting" else "Talk with Juniper"
	if is_instance_valid(voice_hint):
		voice_hint.text = "Voice off · headphones recommended" if state_name == "off" else detail if not detail.is_empty() else "Live voice · talk while you play"
		voice_hint.add_theme_color_override("font_color",LIME if state_name == "connected" else MUTED)

func _voice_context(speak: bool = false, extra: Dictionary = {}) -> void:
	if not is_instance_valid(voice): return
	var context := {"record_id":str(last_record.get("id","")),"phase":"paused" if paused else stage,"screen":screen,"playing":is_playing and not paused,"drill":current_drill if is_playing else str(last_record.get("drill","")),"active_settings":current_round_settings if is_playing else {},"mode_title":_drill_title(current_drill,current_round_settings) if is_playing else _drill_title(str(last_record.get("drill","")),last_record.get("settings",{})),"profile_settings":settings,"cue":str(recommendation.get("cue","")),"speak":speak}
	context.merge(extra,true)
	await _api("/voice/context",HTTPClient.METHOD_POST,context)

func _send_live_measurements() -> void:
	sending_live = true
	var shots := int(live_info.get("shots",0))
	var live := {"elapsed_s":float(live_info.get("elapsed_s",0)),"remaining_s":float(live_info.get("remaining_s",0)),"hits":int(live_info.get("hits",0)),"shots":shots,"accuracy_pct":float(live_info.get("accuracy",0)) if shots > 0 else null,"tracking_on_target_pct":float(live_info.get("tracking_pct",0)) if current_drill == "tracking" else null,"tracking_basis":"active_round_time_including_unheld_time","snapshot_unix_s":Time.get_unix_time_from_system()}
	await _api("/voice/context",HTTPClient.METHOD_POST,{"playing":true,"phase":stage,"drill":current_drill,"live_measurements":live,"active_settings":current_round_settings,"profile_settings":settings})
	sending_live = false

func _poll_controls() -> void:
	controls_polling = true
	var response := await _api("/controls?after="+str(controls_after))
	for action in response.get("actions",[]):
		if int(action.get("seq",0)) <= controls_after: continue
		controls_after = int(action.get("seq",0))
		_apply_control(action)
	controls_after = maxi(controls_after,int(response.get("last_seq",controls_after)))
	controls_polling = false

func _apply_control(action: Dictionary) -> void:
	var result := {"status":"failed","error":"Unknown app control."}
	if float(action.get("expires_at",0)) <= Time.get_unix_time_from_system():
		result = {"status":"failed","error":"This command expired before the game could apply it."}
	elif str(action.get("type","")) == "volume":
		var channel := str(action.get("channel",""))
		if channel in ["voice","music","game"]:
			_set_audio_volume(channel,float(action.get("value",100)))
			result = {"status":"completed","value":_audio_levels()[channel]} if last_settings_save_error.is_empty() else {"status":"failed","error":last_settings_save_error,"value":_audio_levels()[channel]}
	elif str(action.get("type","")) == "music":
		var playback: Dictionary = {}
		match str(action.get("action","")):
			"play": playback = await music.play_track(action.get("track",{}),float(action.get("expires_at",0)))
			"pause": playback = music.pause_music()
			"resume": playback = music.resume_music()
			"stop": playback = music.stop_music()
			"next": playback = await music.next_track(float(action.get("expires_at",0)))
		if playback.get("ok",false): result = {"status":"completed","message":"Music " + str(playback.get("state","playing")) + "."}
		else: result = {"status":"failed","error":str(playback.get("error","Music command failed."))}
	var ack_path := "/controls/"+str(action.get("id",""))+"/ack"
	for attempt in range(2):
		var response := await _api(ack_path,HTTPClient.METHOD_POST,result)
		if response.get("error") == null: return
		if Time.get_unix_time_from_system() >= float(action.get("expires_at",0)): break
	push_warning("Could not acknowledge app control %s; action was not repeated." % str(action.get("id","")))
