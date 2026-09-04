// This file is part of SymCC.
//
// Process-safe JSONL publication for compiler transformation evidence.

#pragma once

#include <llvm/ADT/ArrayRef.h>
#include <llvm/ADT/StringRef.h>

#include <cerrno>
#include <fcntl.h>
#include <string>
#include <sys/file.h>
#include <unistd.h>

namespace symcc {

inline bool appendManifestRecords(
    llvm::StringRef path, llvm::ArrayRef<std::string> records) {
  if (path.empty() || records.empty())
    return true;

  std::string payload;
  for (const std::string &record : records) {
    payload.append(record);
    payload.push_back('\n');
  }

  const std::string storage = path.str();
  int descriptor;
  do {
    descriptor =
        ::open(storage.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0666);
  } while (descriptor < 0 && errno == EINTR);
  if (descriptor < 0)
    return false;

  int lockResult;
  do {
    lockResult = ::flock(descriptor, LOCK_EX);
  } while (lockResult < 0 && errno == EINTR);
  if (lockResult < 0) {
    ::close(descriptor);
    return false;
  }

  bool written = true;
  const char *cursor = payload.data();
  size_t remaining = payload.size();
  while (remaining != 0) {
    ssize_t count = ::write(descriptor, cursor, remaining);
    if (count < 0 && errno == EINTR)
      continue;
    if (count <= 0) {
      written = false;
      break;
    }
    cursor += count;
    remaining -= static_cast<size_t>(count);
  }

  if (written) {
    int syncResult;
    do {
      syncResult = ::fsync(descriptor);
    } while (syncResult < 0 && errno == EINTR);
    written = syncResult == 0;
  }

  int unlockResult;
  do {
    unlockResult = ::flock(descriptor, LOCK_UN);
  } while (unlockResult < 0 && errno == EINTR);
  const int closeResult = ::close(descriptor);
  return written && unlockResult == 0 && closeResult == 0;
}

inline bool appendManifestRecord(
    llvm::StringRef path, std::string record) {
  return appendManifestRecords(path, llvm::ArrayRef<std::string>(record));
}

} // namespace symcc
