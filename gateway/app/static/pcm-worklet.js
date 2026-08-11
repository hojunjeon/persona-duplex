class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = options.processorOptions?.targetSampleRate || 16000;
    this.ratio = sampleRate / this.targetRate;
    this.source = [];
    this.readPos = 0;
    this.output = new Int16Array(320); // 20 ms at 16 kHz
    this.outputPos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length || !input[0]) return true;
    const channels = input.length;
    const length = input[0].length;
    for (let i = 0; i < length; i++) {
      let sample = 0;
      for (let c = 0; c < channels; c++) sample += input[c][i] || 0;
      this.source.push(sample / channels);
    }

    while (this.readPos + 1 < this.source.length) {
      const leftIndex = Math.floor(this.readPos);
      const frac = this.readPos - leftIndex;
      const left = this.source[leftIndex];
      const right = this.source[leftIndex + 1];
      const sample = left + (right - left) * frac;
      const clipped = Math.max(-1, Math.min(1, sample));
      this.output[this.outputPos++] = clipped < 0 ? clipped * 32768 : clipped * 32767;
      this.readPos += this.ratio;

      if (this.outputPos === this.output.length) {
        let sum = 0;
        for (let i = 0; i < this.output.length; i++) {
          const normalized = this.output[i] / 32768;
          sum += normalized * normalized;
        }
        const rms = Math.sqrt(sum / this.output.length);
        const packet = this.output.buffer;
        this.port.postMessage({ type: "pcm", pcm: packet, rms }, [packet]);
        this.output = new Int16Array(320);
        this.outputPos = 0;
      }
    }

    const consumed = Math.floor(this.readPos);
    if (consumed > 0) {
      this.source = this.source.slice(consumed);
      this.readPos -= consumed;
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
