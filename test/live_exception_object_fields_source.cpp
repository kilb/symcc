// RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry exception_object_fields_source
// RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 00 --expect-values 7,47 --expect-cleanup-exception --expect-exception-ops --expect-typed-exception --expect-exception-lifecycle --expect-exception-object-arena --expect-exception-object-fields

struct ExceptionPayload {
  unsigned first;
  unsigned entries[2];
};

extern "C" unsigned long exception_object_fields_source(bool should_throw) {
  if (!should_throw)
    return 7;

  try {
    throw ExceptionPayload{5, {19, 23}};
  } catch (const ExceptionPayload &payload) {
    return static_cast<unsigned long>(
        payload.first + payload.entries[0] + payload.entries[1]);
  }
}
