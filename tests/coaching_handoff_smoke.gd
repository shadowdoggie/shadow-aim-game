extends SceneTree
## No inference or audio: exercise accepted-job acknowledgement and stale responses.

class FakeVoice:
	extends Node
	var is_active := true
	var state := "connected"

class ReviewHost:
	extends "res://game/main.gd"
	var calls: Array[Dictionary] = []
	var announcements: Array[Dictionary] = []
	var replies: Dictionary = {}
	var delays: Dictionary = {}
	var started: Array[Dictionary] = []

	func _ready() -> void:
		pass

	func _process(_delta: float) -> void:
		pass

	func _results(_message: String = "") -> void:
		screen = "results"

	func _begin_round(drill: String, config: Dictionary) -> void:
		started.append({"drill":drill,"settings":config.duplicate(true)})
		is_playing = true

	func _voice_context(speak: bool = false, extra: Dictionary = {}) -> void:
		if speak: announcements.append(extra.duplicate(true))

	func _api(path: String, method: int = HTTPClient.METHOD_GET, payload: Dictionary = {}) -> Dictionary:
		calls.append({"path":path,"method":method,"payload":payload.duplicate(true)})
		var reply: Dictionary = replies.get(path,{"ok":true}).duplicate(true)
		if float(delays.get(path,0)) > 0: await get_tree().create_timer(float(delays[path])).timeout
		return reply


func _initialize() -> void:
	call_deferred("run")


func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("COACHING_HANDOFF_FAIL: " + message)
		quit(1)
	return condition


func run() -> void:
	var host := ReviewHost.new()
	root.add_child(host)
	host.voice = FakeVoice.new()
	host.add_child(host.voice)
	host.screen = "results"
	host.last_record = {"id":"round-one","drill":"clicking","settings":host.settings.duplicate(true)}
	host.last_report = {"record_id":"round-one"}
	host.replies = {"/coach":{"job_id":"job-one"},"/jobs/job-one":{"status":"pending","error":null}}
	await host._request_coaching()
	if not check(host.announcements.size() == 1 and host.announcements[0] == {"record_id":"round-one","review_pending_job_id":"job-one"},"Only an accepted matching job triggers the server-validated pending acknowledgement"): return
	if not check(host.recommendation.is_empty() and host._review_status_text().contains("your round is saved"),"Pending state has honest status without invented coaching"): return
	await host._request_coaching()
	if not check(host.calls.size() == 1 and host.announcements.size() == 1,"Repeated requests cannot duplicate the job or its acknowledgement"): return
	await host.poll_job()
	if not check(host.job_id == "job-one" and host.announcements.size() == 1,"Polling pending state remains silent"): return
	host.replies["/jobs/job-one"] = {"status":"complete","result":{"cue":"Use the recorded review."}}
	await host.poll_job()
	if not check(host.job_id.is_empty() and host.announcements.size() == 2 and host.announcements[1].has("coaching"),"Completed coaching is announced separately from the pending acknowledgement"): return
	host.announcements.clear()
	host.last_record.id = "round-two"
	host.last_report.record_id = "round-two"
	host.replies["/coach"] = {"job_id":"job-two"}
	host.delays["/coach"] = 0.04
	host._request_coaching()
	host._practice_while_reviewing()
	if not check(host.started.size() == 1 and host.started[0].settings == host.last_record.settings and host.submitting_coach,"Same-settings practice must start before review submission returns"): return
	host.last_record.id = "round-three"
	host.last_report.record_id = "round-three"
	await create_timer(0.08).timeout
	if not check(host.job_id == "job-two" and host.announcements.is_empty() and host.calls[-1].path == "/coach","Moving on must preserve a background review without announcing the wrong round"): return
	var before_calls := host.calls.size()
	await host._request_coaching()
	if not check(host.calls.size() == before_calls,"Extra rounds must not submit another review while the first is pending"): return
	host.recommendation = {"cue":"Newer active prescription"}
	host.recommendation_record_id = "newer-approved-record"
	# A running round can still display the reviewed record as last_record.
	host.last_record.id = host.job_record_id
	host.replies["/jobs/job-two"] = {"status":"complete","result":{"cue":"Older background result"}}
	await host.poll_job()
	if not check(host.job_id.is_empty() and host.recommendation.cue == "Newer active prescription" and host.recommendation_record_id == "newer-approved-record" and host.announcements.is_empty(),"A background result must not overwrite the active newer plan"): return
	host.is_playing = false
	host.delays.clear()
	host.replies["/coach"] = {"job_id":"job-three"}
	await host._request_coaching()
	host.announcements.clear()
	host.replies["/jobs/job-three"] = {"status":"complete","result":{"cue":"Stale result"}}
	host.delays["/jobs/job-three"] = 0.05
	host.poll_job()
	await host._cancel_coaching()
	await create_timer(0.08).timeout
	if not check(host.job_id.is_empty() and host.announcements.is_empty() and host.recommendation.is_empty(),"Cancelling immediately invalidates an in-flight completed response"): return
	host.delays.clear()
	host.voice.is_active = false
	host.replies["/coach"] = {"job_id":"job-four"}
	await host._request_coaching()
	if not check(host.announcements.is_empty(),"Reviewing does not activate an opted-out microphone or voice session"): return
	print("COACHING_HANDOFF_SMOKE_PASS: immediate practice, one persistent background review, no wrong-round speech or active-plan replacement, explicit cancellation")
	root.remove_child(host)
	host.queue_free()
	await process_frame
	quit()
