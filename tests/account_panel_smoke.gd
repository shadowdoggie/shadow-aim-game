extends SceneTree
## Stubbed account state only: no real login, browser, clipboard, or account changes.

class AccountView:
	extends "res://game/account_panel.gd"
	var opened_urls: Array[String] = []
	func _open_url(url: String) -> Error:
		opened_urls.append(url)
		return OK

class AccountHost:
	extends Node
	var is_playing := false
	var is_replaying := false
	var screen := "settings"
	var page: VBoxContainer
	var page_generation := 0
	var response: Dictionary = {}
	var delay := 0.0
	var calls: Array[Dictionary] = []

	func _ready() -> void:
		page = VBoxContainer.new()
		add_child(page)

	func _clear_page() -> VBoxContainer:
		page_generation += 1
		for child in page.get_children():
			page.remove_child(child)
			child.queue_free()
		return page

	func _label(parent: Node, value: String, _size: int = 18, _color: Color = Color.WHITE) -> Label:
		var label := Label.new()
		label.text = value
		parent.add_child(label)
		return label

	func _paragraph(parent: Node, value: String, size: int = 18, color: Color = Color.WHITE) -> Label:
		return _label(parent,value,size,color)

	func _card(parent: Node) -> VBoxContainer:
		var card := VBoxContainer.new()
		parent.add_child(card)
		return card

	func _button(parent: Node, value: String, action: Callable, _primary: bool = false) -> Button:
		var button := Button.new()
		button.text = value
		button.pressed.connect(action)
		parent.add_child(button)
		return button

	func _settings_page() -> void:
		screen = "settings"
		_clear_page()

	func _api(path: String, method: int = HTTPClient.METHOD_GET, payload: Dictionary = {}) -> Dictionary:
		calls.append({"path":path,"method":method,"payload":payload})
		var result := response.duplicate(true)
		if delay > 0: await get_tree().create_timer(delay).timeout
		return result


func _initialize() -> void:
	call_deferred("run")


func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("ACCOUNT_PANEL_FAIL: " + message)
		quit(1)
	return condition


func visible_text(node: Node) -> String:
	var text := ""
	if node is Label or node is Button: text += str(node.text) + "\n"
	for child in node.get_children(): text += visible_text(child)
	return text


func run() -> void:
	var host := AccountHost.new()
	root.add_child(host)
	var panel := AccountView.new()
	host.add_child(panel)
	panel.setup(host)
	host.response = {"state":"signed_in","account":{"email":"player@example.invalid","plan_type":"plus"},
		"login":null,"source":"existing_codex","error":null,"availability_note":"Availability depends on this account.",
		"access_token":"never-render-this-token"}
	await panel.show_page()
	var text := visible_text(host.page)
	if not check(text.contains("Connected with ChatGPT") and text.contains("Plus"),"Connected state and reported plan are shown"): return
	if not check(not text.contains("never-render-this-token") and panel.opened_urls.is_empty(),"Account refresh never exposes tokens or opens a browser"): return
	if not check(host.calls.size() == 1 and host.calls[0].method == HTTPClient.METHOD_GET,"Opening account settings is read-only"): return
	host.response = {"state":"pending","account":null,"source":"app","error":null,
		"login":{"login_id":"private-login-id","url":"https://auth.openai.com/codex/device","user_code":"ABCD-EFGH"}}
	await panel._refresh()
	text = visible_text(host.page)
	if not check(text.contains("ABCD-EFGH") and not text.contains("private-login-id") and not text.contains("https://"),"Device flow shows only the short verification code"): return
	if not check(panel.opened_urls.is_empty(),"Polling pending sign-in never opens a browser"): return
	panel._open_pending_page()
	if not check(panel.opened_urls.size() == 1,"Only explicit open action launches the sign-in page"): return
	await panel._start_login("browser")
	if not check(host.calls[-1].path == "/account/login" and host.calls[-1].payload.flow == "browser" and panel.opened_urls.size() == 2,"Browser login uses explicit endpoint and requested flow"): return
	host.response = {"state":"signed_out","account":null,"source":"app","login":null,"error":null}
	await panel._cancel_login()
	if not check(host.calls[-1].path == "/account/login/cancel" and panel.account_state.state == "signed_out","Cancel updates pending login state"): return
	await panel._logout()
	if not check(host.calls[-1].path == "/account/logout","Sign out requires its explicit action"): return
	if not check(not panel._official_login_url("https://auth.openai.com.evil.invalid/login") and not panel._official_login_url("file:///tmp/login"),"Only official HTTPS sign-in destinations may open"): return
	if not check(panel._official_login_url("https://auth.chatgpt.com/login"),"The UI accepts the companion's alternate official sign-in host"): return
	host.response = {"state":"pending","account":null,"source":"app","error":null,
		"login":{"login_id":"late-login","url":"https://auth.openai.com/codex/device","user_code":null}}
	host.delay = 0.05
	panel._start_login("device")
	host._settings_page()
	await create_timer(0.08).timeout
	if not check(host.screen == "settings" and panel.opened_urls.size() == 2,"Late login responses cannot reopen the browser or replace another page"): return
	print("ACCOUNT_PANEL_SMOKE_PASS")
	root.remove_child(host)
	host.queue_free()
	await process_frame
	quit()
