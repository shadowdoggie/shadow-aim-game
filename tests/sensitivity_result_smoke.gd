extends SceneTree
## Cached-result UI regression: stubbed API, no inference, no microphone, no profile writes.

class ResultHost:
	extends "res://game/main.gd"
	var calls: Array[Dictionary] = []
	var saved_result: Dictionary = {}
	var load_delay := 0.0
	var settings_writes := 0

	func _load_settings() -> void:
		pass

	func _save_settings() -> void:
		settings_writes += 1

	func _retry_pending() -> void:
		pass

	func _api(path: String, method: int = HTTPClient.METHOD_GET, _payload: Dictionary = {}) -> Dictionary:
		calls.append({"path":path,"method":method})
		if path == "/health": return {"status":"ok","connection":"ready"}
		if path == "/voice/context": return {"ok":true}
		if path == "/sensitivity/saved-comparison":
			if load_delay > 0: await get_tree().create_timer(load_delay).timeout
			return saved_result.duplicate(true)
		if path == "/sensitivity": return {"experiments":[]}
		return {"error":"No external service in sensitivity-result smoke test"}

var host: ResultHost

func _initialize() -> void:
	call_deferred("run")

func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("SENSITIVITY_RESULT_FAIL: " + message)
		quit(1)
	return condition

func visible_text(node: Node) -> String:
	var text := ""
	if node is Label or node is Button: text += str(node.text) + "\n"
	for child in node.get_children(): text += visible_text(child)
	return text

func run() -> void:
	host = ResultHost.new()
	root.add_child(host)
	await process_frame
	var base: Dictionary = host.settings.duplicate(true)
	base.sensitivity_deg_per_count = 0.065
	var metrics := {"accuracy_pct":95.0,"acquisition_ms":605.0,"click_hits_per_s":1.73,"time_on_target_pct":68.4,"tracking_error_deg":2.34}
	host.saved_result = {
		"experiment":{"id":"saved-comparison","status":"complete","base_settings":base,
			"blocks":[{"candidate_id":"higher"},{"candidate_id":"current"},{"candidate_id":"lower"}],
			"review":{"summary":"Saved coach verdict: try the lower setting.","reason":"Cached model reason: matched rounds support this adjustment.","recommended_candidate_id":"lower","next_step":"Cached model next step: check this in normal practice."}},
		"analysis":{"status":"complete","allowed_candidate_ids":["current","lower"],"recommended_candidate_id":"lower","explanation":"The lower setting showed a small measured improvement.",
			"candidates":[{"id":"lower","sensitivity_deg_per_count":0.05,"metrics":metrics,"comparison_summary":"Tracking aim error improved by 0.03 degrees.","eligible":true}]}}
	await host.sensitivity.load_saved("saved-comparison")
	if not check(host.screen == "sensitivity" and host.sensitivity.experiment.id == "saved-comparison","Saved comparison must reopen its results page"): return
	if not check(host.sensitivity.analysis == host.saved_result.analysis and host.sensitivity.review == host.saved_result.experiment.review,"Reopening must restore cached measurements and AI verdict"): return
	if not check(host.sensitivity.original_settings == base and host.sensitivity.labels == {"higher":"A","current":"B","lower":"C"},"Reopening must restore the experiment settings and blinded labels"): return
	var text := visible_text(host.page)
	for field in ["summary","reason","next_step"]:
		if not check(not text.contains(str(host.saved_result.experiment.review[field])),"Model-generated coaching prose must not appear in the results UI"): return
	if not check(text.contains("95.0% accuracy") and text.contains("2.34°") and text.contains("1.73 hits/sec"),"Results must retain measured values and useful metric precision"): return
	for call in host.calls:
		if not check(not (call.method == HTTPClient.METHOD_POST and (str(call.path).contains("/review") or str(call.path).contains("/coach"))),"Reopening a saved review must not request new inference"): return
	print("PASS saved analysis/review/settings/labels, no AI prose or repeated inference, measured values and metric precision")
	host.sensitivity.review = {}
	host.sensitivity.analysis.explanation = "There is no clear winner. Keep your current setting for now."
	host.sensitivity.analysis.status = "inconclusive"
	host.sensitivity.busy = true
	host.sensitivity.review_started_ms = Time.get_ticks_msec()
	host.sensitivity._show_results()
	text = visible_text(host.page)
	if not check(text.contains("reviewing") and not text.to_lower().contains("no clear winner"),"Pending review must show progress without asserting an early verdict"): return
	host.sensitivity.busy = false
	host.load_delay = 0.08
	host.sensitivity.load_saved("saved-comparison")
	host._home()
	await create_timer(0.12).timeout
	if not check(host.screen == "home" and host.sensitivity.analysis.is_empty() and host.sensitivity.review.is_empty(),"A delayed saved-result response must not replace a newer page"): return
	host.benchmarks = {"clicking":{"id":"benchmark","settings":host.settings.duplicate(true)}}
	host.benchmark_reports = {"clicking":{"record_id":"benchmark","benchmark_key":"unchanged"}}
	var settings_before: Dictionary = host.settings.duplicate(true)
	var benchmarks_before: Dictionary = host.benchmarks.duplicate(true)
	var reports_before: Dictionary = host.benchmark_reports.duplicate(true)
	var writes_before := host.settings_writes
	var slider: HSlider
	for control in host.volume_sliders:
		if is_instance_valid(control) and str(control.get_meta("channel","voice")) == "voice": slider = control
	if not check(slider != null,"Voice volume control must be present"): return
	slider.value = 170
	if not check(is_equal_approx(host.voice_volume,1.7) and host.settings_writes == writes_before + 1,"Volume slider must apply and persist its selected level"): return
	if not check(host.settings == settings_before and host.benchmarks == benchmarks_before and host.benchmark_reports == reports_before and not host.settings.has("voice_volume"),"Voice volume must remain outside aim settings and benchmark identity"): return
	if not check(host.voice.state == "off" and not host.voice.is_active and host.voice._microphone == null,"Review navigation and volume changes must not activate the microphone"): return
	print("SENSITIVITY_RESULT_PASS: pending status, stale page response ignored, volume isolated from aim settings, microphone remains off")
	root.remove_child(host)
	host.queue_free()
	await process_frame
	quit()
