extends Node
## Native account controls. Only explicit sign-in actions open a browser.

const LIME = Color("c5f878")
const TEXT = Color("edf3e9")
const POLL_SECONDS := 2.0
var host: Node
var account_state: Dictionary = {}
var message := ""
var busy := false
var _polling := false
var _poll_elapsed := 0.0
var _generation := 0


func setup(owner_node: Node) -> void:
	host = owner_node


func show_page() -> void:
	if host.is_playing or host.is_replaying: return
	_generation += 1
	host.screen = "account"
	_render()
	await _refresh()


func _render() -> void:
	if not is_instance_valid(host) or host.screen != "account": return
	var body: VBoxContainer = host._clear_page()
	host._label(body,"CHATGPT ACCOUNT",14,LIME)
	host._label(body,"Connect your coach.",40)
	host._paragraph(body,"Use your ChatGPT subscription for coaching and Juniper voice.",21)
	var card: VBoxContainer = host._card(body)
	var state := str(account_state.get("state","loading"))
	if state == "signed_in":
		host._label(card,"Connected with ChatGPT",25,LIME)
		var details: Dictionary = account_state.get("account",{}) if account_state.get("account") is Dictionary else {}
		if details.get("email") != null and not str(details.email).is_empty():
			host._paragraph(card,str(details.email),19,TEXT)
		var plan := str(details.get("plan_type",""))
		if not plan.is_empty() and plan not in ["unknown","null"]:
			host._paragraph(card,"Plan reported by ChatGPT: " + plan.replace("_"," ").capitalize(),17)
		if account_state.get("source") == "existing_codex":
			host._paragraph(card,"Using your existing Codex sign-in. Signing out here disconnects Shadow Aim.",16)
		_action(card,"Sign out of Shadow Aim",_logout)
	elif state == "pending":
		host._label(card,"Finish signing in",25,LIME)
		host._paragraph(card,"Complete the sign-in on OpenAI’s page. This screen updates when you are connected.",19,TEXT)
		var login := _login()
		if login.get("user_code") != null and not str(login.user_code).is_empty():
			host._paragraph(card,"Enter this one-time code on the sign-in page:",17)
			host._label(card,str(login.user_code),30,TEXT)
			_action(card,"Copy sign-in code",_copy_user_code)
		_action(card,"Open sign-in page",_open_pending_page,true)
		_action(card,"Cancel sign-in",_cancel_login)
	elif state == "loading":
		host._paragraph(card,"Checking your account…",21)
	else:
		host._label(card,"Sign in with ChatGPT",25,TEXT)
		host._paragraph(card,"Your account’s plan, model access, and remaining usage determine which AI features are available. You can train without signing in.",18)
		_action(card,"Sign in with ChatGPT  ↗",func(): _start_login("browser"),true)
		_action(card,"Sign in with a device code  ↗",func(): _start_login("device"))
	var note = account_state.get("availability_note")
	if note != null and not str(note).is_empty():
		host._paragraph(card,str(note),16)
	if account_state.get("error") != null and not str(account_state.error).is_empty():
		host._paragraph(card,str(account_state.error),17,TEXT)
	if not message.is_empty(): host._paragraph(card,message,17,LIME)
	_action(body,"Refresh account",_refresh)
	host._button(body,"Back to settings",host._settings_page)


func _action(parent: Node, text: String, callback: Callable, primary: bool = false) -> void:
	var button: Button = host._button(parent,text,callback,primary)
	button.disabled = busy


func _login() -> Dictionary:
	return account_state.login if account_state.get("login") is Dictionary else {}


func _process(delta: float) -> void:
	if not is_instance_valid(host) or host.screen != "account" or account_state.get("state") != "pending": return
	_poll_elapsed += delta
	if _poll_elapsed >= POLL_SECONDS and not busy and not _polling:
		_poll_elapsed = 0.0
		_refresh()


func _refresh() -> void:
	if busy or _polling: return
	_polling = true
	var generation := _generation
	var response: Dictionary = await host._api("/account")
	_polling = false
	if generation != _generation or host.screen != "account": return
	if not response.has("state"):
		message = str(response.get("error","Could not check your account. Try refreshing."))
		if account_state.is_empty(): account_state = {"state":"error"}
		_render()
		return
	var changed := account_state != response or not message.is_empty()
	var state_changed: bool = account_state.get("state") != response.get("state")
	account_state = response
	if state_changed and host.has_method("_connect_health"): host._connect_health()
	message = ""
	if changed: _render()


func _start_login(flow: String) -> void:
	if busy or flow not in ["browser","device"]: return
	_generation += 1
	var generation := _generation
	busy = true
	message = "Preparing sign-in…"
	_render()
	var response: Dictionary = await host._api("/account/login",HTTPClient.METHOD_POST,{"flow":flow})
	busy = false
	if generation != _generation or host.screen != "account": return
	_accept_response(response,"Could not start sign-in. Please retry.")
	if account_state.get("state") == "pending":
		# This path runs only from the player's explicit sign-in button.
		_open_pending_page()
	else:
		_render()


func _accept_response(response: Dictionary, fallback: String) -> void:
	if response.has("state"):
		var state_changed: bool = account_state.get("state") != response.get("state")
		account_state = response
		if state_changed and host.has_method("_connect_health"): host._connect_health()
		message = ""
	else:
		message = str(response.get("error",fallback))


func _open_pending_page() -> void:
	if busy or account_state.get("state") != "pending": return
	var url := str(_login().get("url",""))
	if not _official_login_url(url):
		message = "The sign-in page is unavailable. Cancel and start sign-in again."
	elif _open_url(url) != OK:
		message = "The browser could not open. Try Open sign-in page again."
	else:
		message = "Finish signing in in your browser, then return here."
	_render()


func _official_login_url(url: String) -> bool:
	for prefix in ["https://auth.openai.com/","https://chatgpt.com/","https://auth.chatgpt.com/"]:
		if url.begins_with(prefix): return true
	return false


func _open_url(url: String) -> Error:
	return OS.shell_open(url)


func _copy_user_code() -> void:
	var code = _login().get("user_code")
	if code != null and not str(code).is_empty():
		DisplayServer.clipboard_set(str(code))
		message = "Sign-in code copied."
		_render()


func _cancel_login() -> void:
	await _change_account("/account/login/cancel","Cancelling sign-in…","Could not cancel sign-in. Please retry.")


func _logout() -> void:
	await _change_account("/account/logout","Signing out of Shadow Aim…","Could not sign out. Please retry.")


func _change_account(path: String, pending_message: String, fallback: String) -> void:
	if busy: return
	_generation += 1
	var generation := _generation
	busy = true
	message = pending_message
	_render()
	var response: Dictionary = await host._api(path,HTTPClient.METHOD_POST,{})
	busy = false
	if generation != _generation or host.screen != "account": return
	_accept_response(response,fallback)
	_render()
