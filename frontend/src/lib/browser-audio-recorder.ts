export type AudioRecorderState = "idle" | "recording" | "paused";

interface AudioTrackLike {
  stop(): void;
}

export interface AudioStreamLike {
  getTracks(): AudioTrackLike[];
}

interface AudioDataEventLike {
  data: Blob;
}

export interface MediaRecorderLike {
  state: "inactive" | "recording" | "paused";
  readonly mimeType: string;
  ondataavailable: ((event: AudioDataEventLike) => void) | null;
  onstop: (() => void) | null;
  onerror: ((event: unknown) => void) | null;
  start(): void;
  pause(): void;
  resume(): void;
  stop(): void;
}

export interface AudioRecordingPlatform {
  openMicrophone(): Promise<AudioStreamLike>;
  isMimeTypeSupported(mimeType: string): boolean;
  createMediaRecorder(stream: AudioStreamLike, options?: { mimeType: string }): MediaRecorderLike;
}

export interface AudioRecordingPreview {
  file: File;
  objectUrl: string;
  durationSeconds: number;
}

interface ObjectUrlPlatform {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
}

export function createAudioRecordingPreview(
  file: File,
  durationSeconds: number,
  objectUrls: ObjectUrlPlatform = URL,
): AudioRecordingPreview {
  return { file, durationSeconds, objectUrl: objectUrls.createObjectURL(file) };
}

export function settleAudioRecordingPreview(
  preview: AudioRecordingPreview,
  transcriptionSucceeded: boolean,
  objectUrls: ObjectUrlPlatform = URL,
): AudioRecordingPreview | null {
  if (!transcriptionSucceeded) return preview;
  objectUrls.revokeObjectURL(preview.objectUrl);
  return null;
}

export const AUDIO_RECORDING_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/mp4",
  "audio/ogg;codecs=opus",
] as const;

export function selectAudioRecordingMimeType(
  isSupported: (mimeType: string) => boolean,
): string | undefined {
  return AUDIO_RECORDING_MIME_TYPES.find(isSupported);
}

export class AudioRecordingUnsupportedError extends Error {
  constructor() {
    super("Audio recording is not supported by this browser");
    this.name = "AudioRecordingUnsupportedError";
  }
}

export class AudioRecorderNotActiveError extends Error {
  constructor() {
    super("Audio recorder is not active");
    this.name = "AudioRecorderNotActiveError";
  }
}

export class EmptyAudioRecordingError extends Error {
  constructor() {
    super("Audio recording is empty");
    this.name = "EmptyAudioRecordingError";
  }
}

export class AudioRecordingFailedError extends Error {
  constructor() {
    super("Audio recording failed");
    this.name = "AudioRecordingFailedError";
  }
}

export function audioRecordingErrorMessage(error: unknown): string {
  if (error instanceof AudioRecordingUnsupportedError) {
    return "이 브라우저에서는 음성 녹음을 사용할 수 없습니다.";
  }
  if (error instanceof EmptyAudioRecordingError) {
    return "녹음된 음성이 없습니다. 다시 녹음해 주세요.";
  }
  const errorName = error instanceof Error ? error.name : "";
  if (errorName === "NotAllowedError" || errorName === "SecurityError") {
    return "마이크 권한을 허용해 주세요.";
  }
  if (errorName === "NotFoundError" || errorName === "DevicesNotFoundError") {
    return "사용할 수 있는 마이크를 찾지 못했습니다.";
  }
  if (errorName === "NotReadableError" || errorName === "TrackStartError") {
    return "마이크가 다른 프로그램에서 사용 중인지 확인해 주세요.";
  }
  return "음성 녹음을 완료하지 못했습니다.";
}

function browserAudioRecordingPlatform(): AudioRecordingPlatform {
  if (
    typeof navigator === "undefined" ||
    typeof navigator.mediaDevices?.getUserMedia !== "function" ||
    typeof MediaRecorder === "undefined"
  ) {
    throw new AudioRecordingUnsupportedError();
  }

  return {
    openMicrophone: () => navigator.mediaDevices.getUserMedia({ audio: true }),
    isMimeTypeSupported: (mimeType) =>
      typeof MediaRecorder.isTypeSupported === "function" &&
      MediaRecorder.isTypeSupported(mimeType),
    createMediaRecorder: (stream, options) =>
      options
        ? new MediaRecorder(stream as MediaStream, options)
        : new MediaRecorder(stream as MediaStream),
  };
}

export function extensionForMimeType(mimeType: string): string {
  if (mimeType.includes("mp4")) return "m4a";
  if (mimeType.includes("ogg")) return "ogg";
  if (mimeType.includes("mpeg")) return "mp3";
  if (mimeType.includes("wav")) return "wav";
  if (mimeType.includes("webm")) return "webm";
  return "audio";
}

export class BrowserAudioRecorder {
  readonly #platform: AudioRecordingPlatform;
  #state: AudioRecorderState = "idle";
  #stream: AudioStreamLike | null = null;
  #recorder: MediaRecorderLike | null = null;
  #chunks: Blob[] = [];

  constructor(platform?: AudioRecordingPlatform) {
    this.#platform = platform ?? browserAudioRecordingPlatform();
  }

  get state(): AudioRecorderState {
    return this.#state;
  }

  async start(): Promise<void> {
    if (this.#state === "recording") return;
    if (this.#state === "paused" && this.#recorder) {
      this.#recorder.resume();
      this.#state = "recording";
      return;
    }

    const stream = await this.#platform.openMicrophone();
    try {
      const selectedMimeType = selectAudioRecordingMimeType((mimeType) =>
        this.#platform.isMimeTypeSupported(mimeType),
      );
      const recorder = this.#platform.createMediaRecorder(
        stream,
        selectedMimeType ? { mimeType: selectedMimeType } : undefined,
      );
      this.#stream = stream;
      this.#recorder = recorder;
      this.#chunks = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) this.#chunks.push(event.data);
      };
      recorder.start();
      this.#state = "recording";
    } catch (error) {
      stream.getTracks().forEach((track) => track.stop());
      throw error;
    }
  }

  pause(): void {
    if (this.#state !== "recording" || !this.#recorder) return;
    this.#recorder.pause();
    this.#state = "paused";
  }

  stop(): Promise<File> {
    const recorder = this.#recorder;
    if (this.#state === "idle" || !recorder) {
      return Promise.reject(new AudioRecorderNotActiveError());
    }

    return new Promise<File>((resolve, reject) => {
      recorder.onstop = () => {
        const mimeType = recorder.mimeType || this.#chunks.find((chunk) => chunk.type)?.type || "";
        const audio = new File(
          this.#chunks,
          `eron-recording-${Date.now()}.${extensionForMimeType(mimeType)}`,
          { type: mimeType },
        );
        this.#reset();
        if (audio.size === 0) {
          reject(new EmptyAudioRecordingError());
          return;
        }
        resolve(audio);
      };
      recorder.onerror = () => {
        this.#reset();
        reject(new AudioRecordingFailedError());
      };
      try {
        recorder.stop();
      } catch (error) {
        this.#reset();
        reject(error);
      }
    });
  }

  dispose(): void {
    try {
      if (this.#recorder?.state !== "inactive") this.#recorder?.stop();
    } finally {
      this.#reset();
    }
  }

  #reset(): void {
    if (this.#recorder) {
      this.#recorder.ondataavailable = null;
      this.#recorder.onstop = null;
      this.#recorder.onerror = null;
    }
    this.#stream?.getTracks().forEach((track) => track.stop());
    this.#stream = null;
    this.#recorder = null;
    this.#chunks = [];
    this.#state = "idle";
  }
}
