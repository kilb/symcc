// Linux copy-on-write snapshots for the incremental QF_BV backend.

#include <z3++.h>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

constexpr std::uint32_t kWireMagic = 0x53464331U;
constexpr std::size_t kMaximumLineBytes = 4U * 1024U * 1024U;
constexpr std::size_t kMaximumResponseBytes = 8U * 1024U * 1024U;
constexpr unsigned kMaximumTimeoutMs = 3'600'000U;

enum class ChildStatus : std::uint32_t {
  kSat = 1,
  kUnsat = 2,
  kUnknown = 3,
  kError = 4,
};

struct ChildWireHeader {
  std::uint32_t magic;
  std::uint32_t status;
  std::uint64_t model_bytes;
  std::uint64_t solve_us;
  std::uint64_t minor_faults;
  std::uint64_t major_faults;
  std::uint64_t max_rss_kib;
};

struct ChildResult {
  ChildStatus status = ChildStatus::kError;
  std::vector<std::uint8_t> model;
  std::uint64_t solve_us = 0;
  std::uint64_t minor_faults = 0;
  std::uint64_t major_faults = 0;
  std::uint64_t max_rss_kib = 0;
  pid_t pid = -1;
  bool timed_out = false;
  std::uint64_t roundtrip_us = 0;
};

std::string trim(const std::string &value) {
  const auto first = value.find_first_not_of(" \t\r\n");
  if (first == std::string::npos) {
    return "";
  }
  const auto last = value.find_last_not_of(" \t\r\n");
  return value.substr(first, last - first + 1U);
}

bool starts_with(const std::string &value, const std::string &prefix) {
  return value.size() >= prefix.size() &&
         value.compare(0U, prefix.size(), prefix) == 0;
}

bool all_decimal(const std::string &value) {
  return !value.empty() &&
         std::all_of(value.begin(), value.end(), [](const unsigned char item) {
           return item >= static_cast<unsigned char>('0') &&
                  item <= static_cast<unsigned char>('9');
         });
}

unsigned parse_bounded_unsigned(const std::string &value, unsigned maximum,
                                const char *name) {
  if (!all_decimal(value)) {
    throw std::runtime_error(std::string(name) + " must be decimal");
  }
  std::size_t consumed = 0;
  const auto parsed = std::stoull(value, &consumed, 10);
  if (consumed != value.size() || parsed > maximum) {
    throw std::runtime_error(std::string(name) + " is out of range");
  }
  return static_cast<unsigned>(parsed);
}

std::string status_name(const ChildStatus status) {
  switch (status) {
  case ChildStatus::kSat:
    return "sat";
  case ChildStatus::kUnsat:
    return "unsat";
  case ChildStatus::kUnknown:
  case ChildStatus::kError:
    return "unknown";
  default:
    return "unknown";
  }
}

std::string status_name(const z3::check_result &status) {
  if (status == z3::sat) {
    return "sat";
  }
  if (status == z3::unsat) {
    return "unsat";
  }
  return "unknown";
}

void write_all(const int descriptor, const void *data, std::size_t size) {
  const auto *cursor = static_cast<const std::uint8_t *>(data);
  while (size > 0U) {
    const auto written = ::write(descriptor, cursor, size);
    if (written < 0) {
      if (errno == EINTR) {
        continue;
      }
      _exit(121);
    }
    if (written == 0) {
      _exit(122);
    }
    cursor += static_cast<std::size_t>(written);
    size -= static_cast<std::size_t>(written);
  }
}

std::uint64_t nonnegative_usage(const long value) {
  return value < 0 ? 0U : static_cast<std::uint64_t>(value);
}

class ForkServer {
public:
  explicit ForkServer(const unsigned child_start_delay_ms)
      : solver_(context_, "QF_BV"), empty_sorts_(context_),
        declarations_(context_), child_start_delay_ms_(child_start_delay_ms) {}

  int run() {
    std::string raw_line;
    while (std::getline(std::cin, raw_line)) {
      if (raw_line.size() > kMaximumLineBytes) {
        emit_error("command exceeds 4 MiB");
        return 2;
      }
      if (raw_line.find('\0') != std::string::npos) {
        emit_error("command contains NUL");
        return 2;
      }
      const std::string line = trim(raw_line);
      if (line.empty() || line[0] == ';') {
        continue;
      }
      try {
        if (!dispatch(line)) {
          return 0;
        }
      } catch (const z3::exception &error) {
        emit_error(error.msg());
      } catch (const std::exception &error) {
        emit_error(error.what());
      }
    }
    return 0;
  }

private:
  struct Transaction {
    bool active = false;
    bool check_seen = false;
    bool model_requested = false;
    std::vector<z3::expr> assertions;
    std::vector<std::string> model_symbols;
  };

  bool dispatch(const std::string &line) {
    if (line == "(set-logic QF_BV)" ||
        line == "(set-option :produce-models true)") {
      return true;
    }
    if (starts_with(line, "(set-option :timeout ")) {
      set_timeout(line);
      return true;
    }
    if (line == "(set-option :produce-learned-literals true)") {
      throw std::runtime_error(
          "native state fork does not publish cvc5 learned literals");
    }
    if (starts_with(line, "(declare-fun ")) {
      declare_input(line);
      return true;
    }
    if (starts_with(line, "(assert ")) {
      add_assertion(line);
      return true;
    }
    if (line == "(push 1)") {
      if (transaction_.active) {
        throw std::runtime_error("nested target transaction is unsupported");
      }
      transaction_ = Transaction{};
      transaction_.active = true;
      return true;
    }
    if (line == "(check-sat)") {
      if (!transaction_.active || transaction_.check_seen) {
        throw std::runtime_error("check-sat is outside a target transaction");
      }
      transaction_.check_seen = true;
      return true;
    }
    if (starts_with(line, "(get-value (")) {
      parse_get_value(line);
      return true;
    }
    if (line == "(pop 1)") {
      if (!transaction_.active || !transaction_.check_seen) {
        throw std::runtime_error("pop is outside a checked target transaction");
      }
      const ChildResult result = solve_target();
      emit_result(result);
      transaction_ = Transaction{};
      return true;
    }
    if (starts_with(line, "(echo \"") && line.size() >= 9U &&
        line.substr(line.size() - 2U) == "\")") {
      if (transaction_.active) {
        throw std::runtime_error("echo encountered inside target transaction");
      }
      warm_snapshot();
      const std::string marker = line.substr(7U, line.size() - 9U);
      if (marker.find('"') != std::string::npos ||
          marker.find('\\') != std::string::npos) {
        throw std::runtime_error("unsupported echo marker");
      }
      std::cout << '"' << marker << "\"\n" << std::flush;
      return true;
    }
    if (line == "(exit)") {
      return false;
    }
    throw std::runtime_error("unsupported SMT-LIB command");
  }

  void set_timeout(const std::string &line) {
    constexpr std::size_t prefix_size = sizeof("(set-option :timeout ") - 1U;
    if (line.back() != ')') {
      throw std::runtime_error("malformed timeout option");
    }
    const std::string value = line.substr(
        prefix_size, line.size() - prefix_size - 1U);
    timeout_ms_ = parse_bounded_unsigned(value, kMaximumTimeoutMs, "timeout");
    if (timeout_ms_ == 0U) {
      throw std::runtime_error("timeout must be positive");
    }
  }

  void declare_input(const std::string &line) {
    constexpr const char *suffix = " () (_ BitVec 8))";
    constexpr std::size_t prefix_size = sizeof("(declare-fun ") - 1U;
    const std::size_t suffix_size = std::strlen(suffix);
    if (line.size() <= prefix_size + suffix_size ||
        line.compare(line.size() - suffix_size, suffix_size, suffix) != 0) {
      throw std::runtime_error("only nullary 8-bit input declarations are supported");
    }
    const std::string name = line.substr(
        prefix_size, line.size() - prefix_size - suffix_size);
    constexpr const char *input_prefix = "symcc_input_";
    if (!starts_with(name, input_prefix) ||
        !all_decimal(name.substr(std::strlen(input_prefix)))) {
      throw std::runtime_error("invalid input symbol");
    }
    if (inputs_by_name_.count(name) != 0U) {
      throw std::runtime_error("duplicate input declaration");
    }
    if (inputs_.size() >= 1'048'576U) {
      throw std::runtime_error("input declaration bound exceeded");
    }
    z3::expr input = context_.bv_const(name.c_str(), 8U);
    declarations_.push_back(input.decl());
    inputs_by_name_.emplace(name, inputs_.size());
    inputs_.emplace_back(name, input);
  }

  z3::expr parse_assertion(const std::string &line) {
    constexpr std::size_t prefix_size = sizeof("(assert ") - 1U;
    if (line.size() <= prefix_size || line.back() != ')') {
      throw std::runtime_error("malformed assertion");
    }
    const std::string script = line + "\n";
    z3::expr_vector parsed = context_.parse_string(
        script.c_str(), empty_sorts_, declarations_);
    if (parsed.size() != 1U || !parsed[0].is_bool()) {
      throw std::runtime_error("assertion must parse to one Boolean term");
    }
    return parsed[0];
  }

  void add_assertion(const std::string &line) {
    z3::expr expression = parse_assertion(line);
    if (transaction_.active) {
      if (transaction_.check_seen) {
        throw std::runtime_error("assertion follows check-sat");
      }
      transaction_.assertions.push_back(expression);
      return;
    }
    solver_.add(expression);
    snapshot_dirty_ = true;
  }

  void parse_get_value(const std::string &line) {
    if (!transaction_.active || !transaction_.check_seen ||
        !transaction_.model_symbols.empty()) {
      throw std::runtime_error("invalid get-value position");
    }
    constexpr std::size_t prefix_size = sizeof("(get-value (") - 1U;
    if (line.size() < prefix_size + 2U ||
        line.substr(line.size() - 2U) != "))") {
      throw std::runtime_error("malformed get-value command");
    }
    const std::string body = line.substr(
        prefix_size, line.size() - prefix_size - 2U);
    std::istringstream input(body);
    std::string symbol;
    while (input >> symbol) {
      if (inputs_by_name_.count(symbol) == 0U) {
        throw std::runtime_error("get-value references an unknown input");
      }
      transaction_.model_symbols.push_back(symbol);
    }
    if (transaction_.model_symbols.empty()) {
      throw std::runtime_error("empty get-value command");
    }
    transaction_.model_requested = true;
  }

  void apply_timeout() {
    z3::params parameters(context_);
    parameters.set("timeout", timeout_ms_);
    solver_.set(parameters);
  }

  void warm_snapshot() {
    if (!snapshot_dirty_) {
      return;
    }
    apply_timeout();
    warm_status_ = status_name(solver_.check());
    ++snapshot_generation_;
    ++warm_checks_;
    snapshot_dirty_ = false;
  }

  [[noreturn]] void run_child(const int descriptor) {
    ChildWireHeader header{};
    header.magic = kWireMagic;
    header.status = static_cast<std::uint32_t>(ChildStatus::kError);
    std::vector<std::uint8_t> model_values;
    const auto started = std::chrono::steady_clock::now();
    try {
      if (child_start_delay_ms_ > 0U) {
        ::usleep(static_cast<useconds_t>(child_start_delay_ms_) * 1000U);
      }
      solver_.push();
      for (const z3::expr &assertion : transaction_.assertions) {
        solver_.add(assertion);
      }
      apply_timeout();
      const z3::check_result result = solver_.check();
      const auto solved = std::chrono::steady_clock::now();
      header.solve_us = static_cast<std::uint64_t>(
          std::chrono::duration_cast<std::chrono::microseconds>(solved - started)
              .count());
      if (result == z3::sat) {
        header.status = static_cast<std::uint32_t>(ChildStatus::kSat);
        const z3::model model = solver_.get_model();
        model_values.reserve(transaction_.model_symbols.size());
        for (const std::string &symbol : transaction_.model_symbols) {
          const auto found = inputs_by_name_.find(symbol);
          if (found == inputs_by_name_.end()) {
            throw std::runtime_error("model symbol disappeared");
          }
          const z3::expr value = model.eval(inputs_[found->second].second, true);
          std::uint64_t numeric = 0;
          if (!value.is_numeral_u64(numeric) || numeric > 0xffU) {
            throw std::runtime_error("model value is not an 8-bit numeral");
          }
          model_values.push_back(static_cast<std::uint8_t>(numeric));
        }
      } else if (result == z3::unsat) {
        header.status = static_cast<std::uint32_t>(ChildStatus::kUnsat);
      } else {
        header.status = static_cast<std::uint32_t>(ChildStatus::kUnknown);
      }
    } catch (...) {
      header.status = static_cast<std::uint32_t>(ChildStatus::kError);
      model_values.clear();
    }
    struct rusage usage {};
    if (::getrusage(RUSAGE_SELF, &usage) == 0) {
      header.minor_faults = nonnegative_usage(usage.ru_minflt);
      header.major_faults = nonnegative_usage(usage.ru_majflt);
      header.max_rss_kib = nonnegative_usage(usage.ru_maxrss);
    }
    header.model_bytes = model_values.size();
    write_all(descriptor, &header, sizeof(header));
    if (!model_values.empty()) {
      write_all(descriptor, model_values.data(), model_values.size());
    }
    ::close(descriptor);
    _exit(header.status == static_cast<std::uint32_t>(ChildStatus::kError)
              ? 120
              : 0);
  }

  ChildResult solve_target() {
    warm_snapshot();
    std::array<int, 2> pipe_descriptors{};
    if (::pipe2(pipe_descriptors.data(), O_CLOEXEC) != 0) {
      throw std::runtime_error("pipe2 failed");
    }
    const auto started = std::chrono::steady_clock::now();
    const pid_t child = ::fork();
    if (child < 0) {
      ::close(pipe_descriptors[0]);
      ::close(pipe_descriptors[1]);
      throw std::runtime_error("fork failed");
    }
    if (child == 0) {
      ::close(pipe_descriptors[0]);
      run_child(pipe_descriptors[1]);
    }
    ::close(pipe_descriptors[1]);
    const int flags = ::fcntl(pipe_descriptors[0], F_GETFL, 0);
    if (flags >= 0) {
      static_cast<void>(
          ::fcntl(pipe_descriptors[0], F_SETFL, flags | O_NONBLOCK));
    }

    ChildResult result;
    result.pid = child;
    ++snapshot_queries_;
    std::vector<std::uint8_t> response;
    response.reserve(sizeof(ChildWireHeader) +
                     transaction_.model_symbols.size());
    bool child_exited = false;
    bool pipe_closed = false;
    int child_status = 0;
    const auto deadline =
        started + std::chrono::milliseconds(timeout_ms_);
    while (!child_exited || !pipe_closed) {
      std::array<std::uint8_t, 4096> buffer{};
      while (true) {
        const auto received =
            ::read(pipe_descriptors[0], buffer.data(), buffer.size());
        if (received > 0) {
          response.insert(response.end(), buffer.begin(),
                          buffer.begin() + received);
          if (response.size() > kMaximumResponseBytes) {
            ::kill(child, SIGKILL);
            static_cast<void>(::waitpid(child, &child_status, 0));
            ::close(pipe_descriptors[0]);
            throw std::runtime_error("child response exceeds 8 MiB");
          }
          continue;
        }
        if (received == 0) {
          pipe_closed = true;
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
          ::kill(child, SIGKILL);
          static_cast<void>(::waitpid(child, &child_status, 0));
          ::close(pipe_descriptors[0]);
          throw std::runtime_error("child pipe read failed");
        }
        break;
      }
      if (!child_exited) {
        const pid_t waited = ::waitpid(child, &child_status, WNOHANG);
        if (waited == child) {
          child_exited = true;
        } else if (waited < 0 && errno != EINTR) {
          ::close(pipe_descriptors[0]);
          throw std::runtime_error("waitpid failed");
        }
      }
      if (child_exited && pipe_closed) {
        break;
      }
      const auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
        ::kill(child, SIGKILL);
        while (::waitpid(child, &child_status, 0) < 0 && errno == EINTR) {
        }
        child_exited = true;
        result.timed_out = true;
        result.status = ChildStatus::kUnknown;
        break;
      }
      const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
          deadline - now);
      const int wait_ms = static_cast<int>(
          std::max<std::int64_t>(1, std::min<std::int64_t>(10, remaining.count())));
      struct pollfd descriptor {
        pipe_descriptors[0], POLLIN | POLLHUP, 0
      };
      const int polled = ::poll(&descriptor, 1, wait_ms);
      if (polled < 0 && errno != EINTR) {
        ::kill(child, SIGKILL);
        while (::waitpid(child, &child_status, 0) < 0 && errno == EINTR) {
        }
        ::close(pipe_descriptors[0]);
        throw std::runtime_error("poll failed");
      }
    }
    ::close(pipe_descriptors[0]);
    result.roundtrip_us = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - started)
            .count());
    if (result.timed_out) {
      return result;
    }
    if (!WIFEXITED(child_status) || response.size() < sizeof(ChildWireHeader)) {
      throw std::runtime_error("child exited without a complete response");
    }
    ChildWireHeader header{};
    std::memcpy(&header, response.data(), sizeof(header));
    if (header.magic != kWireMagic ||
        header.model_bytes > kMaximumResponseBytes - sizeof(header) ||
        response.size() != sizeof(header) + header.model_bytes) {
      throw std::runtime_error("child response header is invalid");
    }
    if (header.status < static_cast<std::uint32_t>(ChildStatus::kSat) ||
        header.status > static_cast<std::uint32_t>(ChildStatus::kError)) {
      throw std::runtime_error("child response status is invalid");
    }
    result.status = static_cast<ChildStatus>(header.status);
    result.solve_us = header.solve_us;
    result.minor_faults = header.minor_faults;
    result.major_faults = header.major_faults;
    result.max_rss_kib = header.max_rss_kib;
    result.model.assign(response.begin() + sizeof(header), response.end());
    if (result.status == ChildStatus::kSat &&
        result.model.size() != transaction_.model_symbols.size()) {
      throw std::runtime_error("child model cardinality is invalid");
    }
    if (result.status != ChildStatus::kSat && !result.model.empty()) {
      throw std::runtime_error("non-SAT child returned a model");
    }
    if (result.status == ChildStatus::kError ||
        WEXITSTATUS(child_status) != 0) {
      throw std::runtime_error("child solver failed");
    }
    return result;
  }

  void emit_result(const ChildResult &result) const {
    std::cout << status_name(result.status) << '\n';
    if (result.status == ChildStatus::kSat && transaction_.model_requested) {
      std::cout << '(';
      for (std::size_t index = 0; index < result.model.size(); ++index) {
        if (index != 0U) {
          std::cout << ' ';
        }
        std::cout << '(' << transaction_.model_symbols[index] << " #x"
                  << std::hex << std::setw(2) << std::setfill('0')
                  << static_cast<unsigned>(result.model[index]) << std::dec
                  << ')';
      }
      std::cout << ")\n";
    }
    std::cout
        << "(symcc-native-state-fork-v1\n"
        << " (snapshot-generation " << snapshot_generation_ << ")\n"
        << " (snapshot-queries " << snapshot_queries_ << ")\n"
        << " (warm-checks " << warm_checks_ << ")\n"
        << " (forked 1)\n"
        << " (child-pid " << result.pid << ")\n"
        << " (child-status " << status_name(result.status) << ")\n"
        << " (child-timed-out " << (result.timed_out ? 1 : 0) << ")\n"
        << " (child-solve-us " << result.solve_us << ")\n"
        << " (fork-roundtrip-us " << result.roundtrip_us << ")\n"
        << " (child-minor-faults " << result.minor_faults << ")\n"
        << " (child-major-faults " << result.major_faults << ")\n"
        << " (child-max-rss-kib " << result.max_rss_kib << ")\n"
        << " (warm-status " << warm_status_ << "))\n";
  }

  static void emit_error(const std::string &message) {
    std::string escaped;
    escaped.reserve(std::min<std::size_t>(message.size(), 512U));
    for (const char item : message) {
      if (escaped.size() >= 512U) {
        break;
      }
      if (item == '"' || item == '\\') {
        escaped.push_back('\\');
      }
      if (item >= 0x20 && item <= 0x7e) {
        escaped.push_back(item);
      }
    }
    std::cout << "(error \"" << escaped << "\")\n" << std::flush;
  }

  z3::context context_;
  z3::solver solver_;
  z3::sort_vector empty_sorts_;
  z3::func_decl_vector declarations_;
  std::vector<std::pair<std::string, z3::expr>> inputs_;
  std::unordered_map<std::string, std::size_t> inputs_by_name_;
  Transaction transaction_;
  unsigned timeout_ms_ = 30'000U;
  unsigned child_start_delay_ms_ = 0U;
  bool snapshot_dirty_ = true;
  std::uint64_t snapshot_generation_ = 0;
  std::uint64_t snapshot_queries_ = 0;
  std::uint64_t warm_checks_ = 0;
  std::string warm_status_ = "unknown";
};

} // namespace

int main(const int argc, char **argv) {
#if !defined(__linux__)
  std::cerr << "symcc-qfbv-z3-forkserver requires Linux\n";
  return 2;
#else
  unsigned child_start_delay_ms = 0U;
  for (int index = 1; index < argc; ++index) {
    const std::string argument(argv[index]);
    if (argument == "--child-start-delay-ms" && index + 1 < argc) {
      try {
        child_start_delay_ms = parse_bounded_unsigned(
            argv[++index], 60'000U, "child start delay");
      } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 2;
      }
      continue;
    }
    if (argument == "--help") {
      std::cout << "usage: symcc-qfbv-z3-forkserver "
                   "[--child-start-delay-ms N]\n";
      return 0;
    }
    std::cerr << "unsupported argument: " << argument << '\n';
    return 2;
  }
  try {
    z3::set_param("parallel.enable", false);
    ForkServer server(child_start_delay_ms);
    return server.run();
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
#endif
}
