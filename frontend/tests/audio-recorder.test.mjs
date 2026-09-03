import assert from "node:assert/strict";
import test from "node:test";

import {
  AudioRecordingUnsupportedError,
  BrowserAudioRecorder,
  EmptyAudioRecordingError,
  audioRecordingErrorMessage,
  createAudioRecordingPreview,
  selectAudioRecordingMimeType,
  settleAudioRecordingPreview,
} from "../src/lib/browser-audio-recorder.ts";

test("지원 MIME type은 webm, mp4, ogg 우선순위로 선택한다", () => {
  assert.equal(
    selectAudioRecordingMimeType((mimeType) =>
      ["audio/mp4", "audio/ogg;codecs=opus"].includes(mimeType),
    ),
    "audio/mp4",
  );
  assert.equal(
    selectAudioRecordingMimeType(() => false),
    undefined,
  );
});

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
    isMimeTypeSupported(mimeType) {
      return mimeType === "audio/webm;codecs=opus";
    },
    createMediaRecorder(receivedStream, options) {
      assert.equal(receivedStream, stream);
      assert.deepEqual(options, { mimeType: "audio/webm;codecs=opus" });
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

test("iPhone 계열 audio/mp4 녹음은 m4a 파일로 생성한다", async () => {
  const stream = { getTracks: () => [{ stop() {} }] };
  const mediaRecorder = {
    state: "inactive",
    mimeType: "audio/mp4",
    ondataavailable: null,
    onstop: null,
    onerror: null,
    start() {
      this.state = "recording";
    },
    pause() {
      this.state = "paused";
    },
    resume() {
      this.state = "recording";
    },
    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["iphone"], { type: "audio/mp4" }) });
      this.onstop?.();
    },
  };
  const recorder = new BrowserAudioRecorder({
    async openMicrophone() {
      return stream;
    },
    isMimeTypeSupported: (mimeType) => mimeType === "audio/mp4",
    createMediaRecorder(_stream, options) {
      assert.deepEqual(options, { mimeType: "audio/mp4" });
      return mediaRecorder;
    },
  });
  await recorder.start();
  const audio = await recorder.stop();
  assert.equal(audio.type, "audio/mp4");
  assert.match(audio.name, /\.m4a$/);
});

test("MIME type을 지정할 수 없으면 기본 MediaRecorder와 실제 chunk 형식을 사용한다", async () => {
  const stream = { getTracks: () => [{ stop() {} }] };
  const mediaRecorder = {
    state: "inactive",
    mimeType: "",
    ondataavailable: null,
    onstop: null,
    onerror: null,
    start() {
      this.state = "recording";
    },
    pause() {},
    resume() {},
    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["browser-default"], { type: "audio/mp4" }) });
      this.onstop?.();
    },
  };
  const recorder = new BrowserAudioRecorder({
    async openMicrophone() {
      return stream;
    },
    isMimeTypeSupported: () => false,
    createMediaRecorder(_stream, options) {
      assert.equal(options, undefined);
      return mediaRecorder;
    },
  });
  await recorder.start();
  const audio = await recorder.stop();
  assert.equal(audio.type, "audio/mp4");
  assert.match(audio.name, /\.m4a$/);
});

test("빈 녹음은 거부하고 마이크 스트림을 정리한다", async () => {
  let stopped = false;
  const mediaRecorder = {
    state: "inactive",
    mimeType: "audio/webm",
    ondataavailable: null,
    onstop: null,
    onerror: null,
    start() {
      this.state = "recording";
    },
    pause() {},
    resume() {},
    stop() {
      this.state = "inactive";
      this.onstop?.();
    },
  };
  const recorder = new BrowserAudioRecorder({
    async openMicrophone() {
      return {
        getTracks: () => [
          {
            stop() {
              stopped = true;
            },
          },
        ],
      };
    },
    isMimeTypeSupported: () => true,
    createMediaRecorder: () => mediaRecorder,
  });
  await recorder.start();
  await assert.rejects(recorder.stop(), EmptyAudioRecordingError);
  assert.equal(stopped, true);
  assert.equal(recorder.state, "idle");
});

test("마이크 권한 거부는 호출자에게 그대로 전달된다", async () => {
  const recorder = new BrowserAudioRecorder({
    async openMicrophone() {
      throw new DOMException("denied", "NotAllowedError");
    },
    isMimeTypeSupported: () => true,
    createMediaRecorder() {
      throw new Error("unreachable");
    },
  });
  await assert.rejects(recorder.start(), { name: "NotAllowedError" });
});

test("다시 녹음 또는 취소 시 이전 Object URL을 정리한다", () => {
  const revoked = [];
  const objectUrls = {
    createObjectURL: () => "blob:recording-1",
    revokeObjectURL: (url) => revoked.push(url),
  };
  const preview = createAudioRecordingPreview(
    new File(["recording"], "recording.m4a", { type: "audio/mp4" }),
    3,
    objectUrls,
  );
  assert.equal(settleAudioRecordingPreview(preview, true, objectUrls), null);
  assert.deepEqual(revoked, ["blob:recording-1"]);
});

test("STT 실패 시 동일 녹음을 유지하고 성공한 뒤에만 정리한다", () => {
  const revoked = [];
  const objectUrls = {
    createObjectURL: () => "blob:retryable-recording",
    revokeObjectURL: (url) => revoked.push(url),
  };
  const preview = createAudioRecordingPreview(
    new File(["recording"], "recording.webm", { type: "audio/webm" }),
    5,
    objectUrls,
  );
  assert.equal(settleAudioRecordingPreview(preview, false, objectUrls), preview);
  assert.deepEqual(revoked, []);
  assert.equal(settleAudioRecordingPreview(preview, true, objectUrls), null);
  assert.deepEqual(revoked, ["blob:retryable-recording"]);
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
