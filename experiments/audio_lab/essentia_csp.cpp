// Research bindings for MusiCNN, beat tracking and EBU R128 loudness.
// Essentia itself is AGPL-3.0; see the pinned source and vendor licence.
#include <emscripten/bind.h>
#include "essentia.h"
#include "algorithmfactory.h"
#include <memory>
#include <cmath>
#include <algorithm>
#include <stdexcept>

class EssentiaJS {
  static std::vector<float> copy(emscripten::val input) {
    const auto length = input["length"].as<unsigned>();
    if (length != 441000) throw std::runtime_error("Expected ten seconds at 44100 Hz");
    std::vector<float> values(length);
    emscripten::val(emscripten::typed_memory_view(length, values.data())).call<void>("set", input);
    return values;
  }

  static void number(emscripten::val& result, const char* key, double value) {
    result.set(key, std::isfinite(value) ? emscripten::val(value) : emscripten::val::null());
  }

  static double finiteMean(const std::vector<float>& values) {
    double sum = 0;
    unsigned count = 0;
    for (float value : values) if (std::isfinite(value)) { sum += value; ++count; }
    return count ? sum / count : NAN;
  }

 public:
  explicit EssentiaJS(bool) {
    essentia::warningLevelActive = false;
    essentia::infoLevelActive = false;
    essentia::init();
  }

  emscripten::val TensorflowInputMusiCNN(std::vector<float>& frame) {
    std::unique_ptr<essentia::standard::Algorithm> algorithm(
        essentia::standard::AlgorithmFactory::create("TensorflowInputMusiCNN"));
    std::vector<float> bands;
    algorithm->input("frame").set(frame);
    algorithm->output("bands").set(bands);
    algorithm->compute();
    auto result = emscripten::val::object();
    result.set("bands", bands);
    return result;
  }

  emscripten::val AudioAttributes(emscripten::val leftInput, emscripten::val rightInput) {
    const bool stereoInput = !rightInput.isNull() && !rightInput.isUndefined();
    auto left = copy(leftInput);
    auto right = stereoInput ? copy(rightInput) : std::vector<float>();
    std::vector<essentia::StereoSample> stereo(left.size());
    std::vector<float> mono(left.size());
    for (unsigned i = 0; i < left.size(); ++i) {
      const float r = stereoInput ? right[i] : 0;
      if (!std::isfinite(left[i]) || !std::isfinite(r)) throw std::runtime_error("Nonfinite audio");
      stereo[i].left() = left[i];
      stereo[i].right() = r;
      mono[i] = stereoInput ? (left[i] + r) * .5f : left[i];
    }
    auto result = emscripten::val::object();
    {
      std::unique_ptr<essentia::standard::Algorithm> algorithm(
          essentia::standard::AlgorithmFactory::create("LoudnessEBUR128", "sampleRate", 44100.));
      std::vector<float> momentary, shortTerm;
      float integrated = 0, range = 0;
      algorithm->input("signal").set(stereo);
      algorithm->output("momentaryLoudness").set(momentary);
      algorithm->output("shortTermLoudness").set(shortTerm);
      algorithm->output("integratedLoudness").set(integrated);
      algorithm->output("loudnessRange").set(range);
      algorithm->compute();
      number(result, "integrated_lufs", integrated);
      number(result, "momentary_lufs_mean", finiteMean(momentary));
      number(result, "short_term_lufs_mean", finiteMean(shortTerm));
      number(result, "excerpt_loudness_range_lu", range);
    }
    {
      std::unique_ptr<essentia::standard::Algorithm> algorithm(
          essentia::standard::AlgorithmFactory::create("RhythmExtractor2013", "method", "multifeature"));
      float bpm = 0, confidence = 0;
      std::vector<float> ticks, estimates, intervals;
      algorithm->input("signal").set(mono);
      algorithm->output("bpm").set(bpm);
      algorithm->output("confidence").set(confidence);
      algorithm->output("ticks").set(ticks);
      algorithm->output("estimates").set(estimates);
      algorithm->output("bpmIntervals").set(intervals);
      algorithm->compute();
      number(result, "tempo_bpm", bpm > 0 ? bpm : NAN);
      number(result, "tempo_confidence", confidence);
      const double average = finiteMean(intervals);
      double variance = 0;
      for (float interval : intervals) variance += (interval - average) * (interval - average);
      number(result, "beat_interval_cv", intervals.size() > 1 && average > 0
          ? std::sqrt(variance / intervals.size()) / average : NAN);
      result.set("beat_count", ticks.size());
    }
    std::fill(left.begin(), left.end(), 0);
    std::fill(right.begin(), right.end(), 0);
    std::fill(mono.begin(), mono.end(), 0);
    for (auto& value : stereo) { value.left() = 0; value.right() = 0; }
    return result;
  }
};

EMSCRIPTEN_BINDINGS(tuneconsole_musicnn) {
  emscripten::register_vector<float>("VectorFloat");
  emscripten::class_<EssentiaJS>("EssentiaJS")
      .constructor<bool>()
      .function("TensorflowInputMusiCNN", &EssentiaJS::TensorflowInputMusiCNN)
      .function("AudioAttributes", &EssentiaJS::AudioAttributes);
}
