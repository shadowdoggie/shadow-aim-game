extends SceneTree
## Local response fixtures only. No account changes, voice or model calls.

class FakeHost:
	extends Node
	var screen := "settings"
	var response := {"available":true,"memories":[{"key":"favorite_game","value":"Fixture game","updated_at":1}],"error":null,"account_generation":"opaque-fixture-generation"}
	var requests: Array[Dictionary] = []
	func _card(parent: Node) -> VBoxContainer:
		var card := VBoxContainer.new()
		parent.add_child(card)
		return card
	func _label(parent: Node, text: String, _size=18) -> Label:
		var label := Label.new()
		label.text = text
		parent.add_child(label)
		return label
	func _paragraph(parent: Node, text: String, _size=18) -> Label:
		return _label(parent,text)
	func _button(parent: Node, text: String, callback: Callable) -> Button:
		var button := Button.new()
		button.text = text
		button.pressed.connect(callback)
		parent.add_child(button)
		return button
	func _api(path: String, method: int = HTTPClient.METHOD_GET, payload: Dictionary = {}) -> Dictionary:
		requests.append({"path":path,"method":method,"payload":payload.duplicate(true)})
		var result: Dictionary = response.duplicate(true)
		await get_tree().process_frame
		return result

func _initialize() -> void:
	call_deferred("run")
	create_timer(5.0).timeout.connect(func(): quit(1))

func run() -> void:
	var host := FakeHost.new()
	root.add_child(host)
	var panel = load("res://game/player_memory_panel.gd").new()
	host.add_child(panel)
	panel.setup(host)
	await process_frame
	await process_frame
	assert(panel.available and panel.memories.size() == 1 and panel.message.is_empty(),"Nullable errors must allow viewing saved notes")
	assert(not panel._clear_button.disabled)
	host.response.account_generation = "opaque-fixture-generation"
	await panel.refresh()
	host.response = {"error":"Fixture network failure"}
	await panel.clear_all()
	assert(panel.memories.size() == 1 and panel.message.contains("Could not clear"),"A failed delete must preserve the displayed memory")
	assert(host.requests.back().path == "/voice/memory/forget" and host.requests.back().method == HTTPClient.METHOD_POST and host.requests.back().payload == {"all":true,"account_generation":"opaque-fixture-generation"},"Clear must carry the snapshot generation so the server can reject account-switch races")
	host.response = {"available":true,"memories":[],"error":null}
	await panel.clear_all()
	assert(panel.memories.is_empty() and panel._clear_button.disabled and panel.message == "Saved memories cleared.","Successful clear must update the note list and action state")
	host.response = {"available":false,"memories":[{"key":"must_not_show","value":"Other account note"}],"error":null}
	await panel.refresh()
	assert(not panel.available and panel.memories.is_empty() and panel._clear_button.disabled,"Unavailable account memory must never show stale or other-account notes")
	var count := host.requests.size()
	await panel.clear_all()
	assert(host.requests.size() == count,"Signed-out users must not submit a forget request")
	var corrupt_message := "Saved memories could not be read. You can clear them in Settings."
	host.response = {"available":false,"memories":[],"count":0,"truncated":false,"error":corrupt_message,"account_generation":"corrupt-fixture-generation"}
	await panel.refresh()
	assert(panel._read_error and panel.message == corrupt_message and not panel._clear_button.disabled,"A scoped corrupt-memory snapshot must show its real recovery message and allow clearing even with zero readable notes")
	for label in panel.find_children("*","Label",true,false):
		assert(not label.text.contains("Sign in"),"Corrupt saved memory must not be presented as a signed-out account")
	host.response = {"available":true,"memories":[],"error":null,"account_generation":"corrupt-fixture-generation"}
	await panel.clear_all()
	assert(host.requests.back().payload == {"all":true,"account_generation":"corrupt-fixture-generation"},"Recovery must preserve account-generation protection")
	assert(not panel._read_error and panel._clear_button.disabled and panel.message == "Saved memories cleared.")
	host.response = {"error":corrupt_message}
	await panel.refresh()
	assert(not panel._read_error and panel._clear_button.disabled,"An HTTP error without a scoped snapshot must not enable destructive recovery")
	count = host.requests.size()
	await panel.clear_all()
	assert(host.requests.size() == count)
	host.response = {"available":true,"memories":[{"key":"late","value":"Late result"}]}
	panel.refresh()
	host.screen = "home"
	await process_frame
	assert(panel.memories.is_empty(),"Leaving settings must discard late memory responses")
	host.queue_free()
	await process_frame
	print("PLAYER_MEMORY_UI_SMOKE_PASS: account-scoped notes, corruption recovery, nullable errors, honest delete failure/success, signed-out controls, stale response guard")
	quit()
