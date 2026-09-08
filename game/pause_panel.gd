extends Node
## A menu overlay only: the SceneTree and live voice keep running while aim is paused.

var active := false
var host: Node
var layer: CanvasLayer
var overlay: Control
var resume_button: Button
var voice_button: Button
var voice_hint: Label

func setup(owner_node: Node) -> void:
	host = owner_node
	layer = CanvasLayer.new()
	layer.layer = 30
	add_child(layer)
	overlay = Control.new()
	overlay.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	overlay.theme = host.page.theme
	layer.add_child(overlay)
	var backdrop := ColorRect.new()
	backdrop.color = Color(0.025, 0.035, 0.04, 0.88)
	backdrop.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	overlay.add_child(backdrop)
	var center := CenterContainer.new()
	center.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	overlay.add_child(center)
	var panel := PanelContainer.new()
	panel.custom_minimum_size = Vector2(540, 0)
	panel.add_theme_stylebox_override("panel", host._box(Color("151e23"), 18, Color("2b383e")))
	center.add_child(panel)
	var body := VBoxContainer.new()
	body.add_theme_constant_override("separation", 16)
	panel.add_child(body)
	host._label(body, "TAKE YOUR TIME", 14, Color("c5f878"))
	host._label(body, "Paused.", 46)
	host._paragraph(body, "Your round is frozen. Resume whenever you are ready.", 18)
	resume_button = host._button(body, "Resume round  →", resume, true)
	voice_button = host._button(body, "Talk with Juniper", host._toggle_voice)
	voice_hint = host._paragraph(body, "You can talk with your coach while paused.", 15)
	if host.has_method("_volume_control"): host._volume_control(body,["voice","music","game"])
	host._button(body, "End round and return", end_round)
	host._label(body, "Escape to resume", 14, Color("98aaa8"))
	if is_instance_valid(host.voice):
		host.voice.status_changed.connect(_voice_status_changed)
	overlay.hide()

func show_pause() -> void:
	active = true
	Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	overlay.show()
	if is_instance_valid(host.voice):
		_voice_status_changed(host.voice.state, host.voice.message)
	resume_button.grab_focus()

func hide_pause() -> void:
	active = false
	if is_instance_valid(overlay): overlay.hide()

func dismiss() -> void:
	hide_pause()

func resume() -> void:
	if not active: return
	hide_pause()
	host._resume_from_pause()

func end_round() -> void:
	if not active: return
	hide_pause()
	host._end_paused_round()

func _voice_status_changed(state_name: String, detail: String) -> void:
	voice_button.text = "Stop Juniper · mic on" if state_name == "connected" else "Cancel voice" if state_name == "connecting" else "Talk with Juniper"
	voice_hint.text = detail if not detail.is_empty() else "You can talk with your coach while paused."
