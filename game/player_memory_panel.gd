extends VBoxContainer
## Account-scoped memory management; this panel never starts voice or inference.

var host: Node
var memories: Array = []
var available := false
var message := ""
var _loaded := false
var _busy := false
var _generation := 0
var _clear_button: Button
var _account_generation: Variant = null
var _truncated := false
var _read_error := false

func setup(owner_node: Node) -> void:
	host = owner_node
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_render()
	refresh()

func _render() -> void:
	for child in get_children():
		remove_child(child)
		child.queue_free()
	var card: VBoxContainer = host._card(self)
	host._label(card,"Juniper’s saved memory",23)
	host._paragraph(card,"Personal notes saved for this ChatGPT account on this computer. Ask Juniper to remember or forget something.",16)
	if not _loaded:
		host._paragraph(card,"Loading saved memories…" if _busy else "Saved memories are unavailable.",17)
	elif _read_error:
		pass # The recoverable server message is shown below the account heading.
	elif not available:
		host._paragraph(card,"Sign in with ChatGPT to view this account’s saved memories.",17)
	elif memories.is_empty():
		host._paragraph(card,"No personal memories saved yet.",17)
	else:
		for item in memories:
			if not item is Dictionary: continue
			var value: String = str(item.get("value",""))
			if value.is_empty(): continue
			var key: String = str(item.get("key",""))
			if not key.is_empty(): host._label(card,key.replace("_"," ").capitalize(),16)
			host._paragraph(card,value,18)
		if _truncated: host._paragraph(card,"Some saved notes are not shown in this view.",16)
	if not message.is_empty(): host._paragraph(card,message,16)
	var actions := HBoxContainer.new()
	actions.add_theme_constant_override("separation",12)
	card.add_child(actions)
	var refresh_button: Button = host._button(actions,"Refresh memories",refresh)
	refresh_button.disabled = _busy
	_clear_button = host._button(actions,"Clear saved memories",clear_all)
	_clear_button.disabled = _busy or not _can_clear()

func _can_clear() -> bool:
	return _account_generation != null and (_read_error or (available and not memories.is_empty()))

func _recoverable_read_error(response: Dictionary) -> bool:
	return response.has("available") and response.available == false and response.get("account_generation") != null and response.get("error") is String and not str(response.error).is_empty()

func _current(generation: int) -> bool:
	return generation == _generation and is_inside_tree() and is_instance_valid(host) and host.screen == "settings"

func refresh(success_message: String = "") -> void:
	if _busy: return
	_generation += 1
	var generation := _generation
	_busy = true
	message = ""
	_render()
	var response: Dictionary = await host._api("/voice/memory")
	if not _current(generation): return
	_busy = false
	if not response.has("available") or (response.get("error") != null and not _recoverable_read_error(response)):
		_loaded = false
		available = false
		_read_error = false
		_account_generation = null
		memories = []
		message = "Could not load saved memories. Please try again."
	else:
		_accept(response)
		message = str(response.error) if _read_error else success_message
	_render()

func _accept(response: Dictionary) -> void:
	_loaded = true
	available = bool(response.get("available",false))
	_account_generation = response.get("account_generation")
	_read_error = _recoverable_read_error(response)
	_truncated = bool(response.get("truncated",false))
	memories = response.get("memories",[]).duplicate(true) if available and response.get("memories") is Array else []

func clear_all() -> void:
	if _busy or not _can_clear(): return
	_generation += 1
	var generation := _generation
	_busy = true
	message = "Clearing saved memories…"
	_render()
	var payload := {"all":true}
	if _account_generation != null: payload.account_generation = _account_generation
	var response: Dictionary = await host._api("/voice/memory/forget",HTTPClient.METHOD_POST,payload)
	if not _current(generation): return
	_busy = false
	if response.get("error") != null:
		message = "Could not clear saved memories. Please try again."
		_render()
		return
	if response.has("available") and response.get("memories") is Array:
		_accept(response)
		message = "Saved memories cleared." if available else "Sign in with ChatGPT to manage saved memories."
		_render()
	else:
		refresh("Saved memories cleared.")

func _exit_tree() -> void:
	_generation += 1
