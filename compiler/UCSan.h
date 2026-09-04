// This file is part of SymCC.
//
// SymCC is free software: you can redistribute it and/or modify it under the
// terms of the GNU General Public License as published by the Free Software
// Foundation, either version 3 of the License, or (at your option) any later
// version.

#ifndef SYMCC_UCSAN_H
#define SYMCC_UCSAN_H

namespace llvm {
class Module;
}

/// Apply compilation-based under-constrained execution instrumentation when
/// SYMCC_UCSAN_CONFIG or SYMCC_UCSAN_ENTRY is set.
bool instrumentUCSan(llvm::Module &M);

#endif
