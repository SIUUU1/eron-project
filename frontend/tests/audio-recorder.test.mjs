import assert from "node:assert/strict";
import test from "node:test";

import {
  AudioRecordingUnsupportedError,
  BrowserAudioRecorder,
  EmptyAudioRecordingError,
  audioRecordingErrorMessage,
} from "../src/lib/browser-audio-recorder.ts";

test("브라우저 녹음은 시작·일시정지·재개 후 하나의 오디오 파일로 종료된다", async () => {
  const calls = [];
  const track = {
    stop() {
      calls.push("track.stop");
    },
  };
  const stream = {
    getTracks() {
      return [track];
    },
  };
  const mediaRecorder = {
    state: "inactive",
    mimeType: "audio/webm;codecs=opus",
    ondataavailable: null,
    onstop: null,
    onerror: null,
    start() {
      this.state = "recording";
      calls.push("recorder.start");
    },
    pause() {
      this.state = "paused";
      calls.push("recorder.pause");
    },
    resume() {
      this.state = "recording";
      calls.push("recorder.resume");
    },
    stop() {
      this.state = "inactive";
      calls.push("recorder.stop");
      this.ondataavailable?.({ data: new Blob(["synthetic-recording"]) });
      this.onstop?.();
    },
  };
  const recorder = new BrowserAudioRecorder({
    async openMicrophone() {
      calls.push("openMicrophone");
      return stream;
    },
    createMediaRecorder(receivedStream) {
      assert.equal(receivedStream, stream);
      return mediaRecorder;
    },
  });

  await recorder.start();
  assert.equal(recorder.state, "recording");

  recorder.pause();
  assert.equal(recorder.state, "paused");

  await recorder.start();
  assert.equal(recorder.state, "recording");

  const audio = await recorder.stop();

  assert.equal(recorder.state, "idle");
  assert.equal(audio.type, "audio/webm;codecs=opus");
  assert.match(audio.name, /^eron-recording-\d+\.webm$/);
  assert.ok(audio.size > 0);
  assert.deepEqual(calls, [
    "openMicrophone",
    "recorder.start",
    "recorder.pause",
    "recorder.resume",
    "recorder.stop",
    "track.stop",
  ]);
});

test("마이크 권한·장치·녹음 오류를 사용자가 이해할 수 있는 안내로 구분한다", () => {
  assert.deepEqual(
    [
      new DOMException("denied", "NotAllowedError"),
      new DOMException("missing", "NotFoundError"),
      new DOMException("busy", "NotReadableError"),
      new AudioRecordingUnsupportedError(),
      new EmptyAudioRecordingError(),
      new Error("unknown"),
    ].map(audioRecordingErrorMessage),
    [
      "마이크 권한을 허용해 주세요.",
      "사용할 수 있는 마이크를 찾지 못했습니다.",
      "마이크가 다른 프로그램에서 사용 중인지 확인해 주세요.",
      "이 브라우저에서는 음성 녹음을 사용할 수 없습니다.",
      "녹음된 음성이 없습니다. 다시 녹음해 주세요.",
      "음성 녹음을 완료하지 못했습니다.",
    ],
  );
});
