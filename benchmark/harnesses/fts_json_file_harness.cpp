#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "json.hpp"

namespace {

int process_json(const uint8_t *data, size_t size) {
  if (data == nullptr || size == 0 || size > (1U << 20))
    return 0;

  try {
    nlohmann::json parsed = nlohmann::json::parse(data, data + size);

    std::string normalized = parsed.dump();
    nlohmann::json reparsed = nlohmann::json::parse(normalized);
    if (reparsed != parsed)
      return 1;

    volatile size_t observed = parsed.size();
    if (parsed.is_object()) {
      for (auto it = parsed.begin(); it != parsed.end(); ++it) {
        observed += it.key().size();
        observed += it.value().dump().size();
      }
    } else if (parsed.is_array()) {
      for (const auto &value : parsed)
        observed += value.dump().size();
    }
    return static_cast<int>(observed & 1U);
  } catch (...) {
    return 0;
  }
}

std::vector<uint8_t> read_file(const char *path) {
  FILE *file = std::fopen(path, "rb");
  if (file == nullptr)
    return {};
  std::fseek(file, 0, SEEK_END);
  long size = std::ftell(file);
  std::fseek(file, 0, SEEK_SET);
  if (size <= 0 || size > (1L << 20)) {
    std::fclose(file);
    return {};
  }
  std::vector<uint8_t> data(static_cast<size_t>(size));
  size_t got = std::fread(data.data(), 1, data.size(), file);
  std::fclose(file);
  data.resize(got);
  return data;
}

} // namespace

int main(int argc, char **argv) {
  if (argc != 2)
    return 1;
  std::vector<uint8_t> data = read_file(argv[1]);
  return process_json(data.data(), data.size());
}
