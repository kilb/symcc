// RUN: rm -f %t.dist
// RUN: env SYMCC_COLOR_TARGETS=target SYMCC_COLORATION_OUT=%t.dist %symcc -g -O0 %s -o %t
// RUN: python3 -c "vals=[float(l.split()[1]) for l in open(r'%t.dist') if l.strip() and not l.startswith('#')]; assert vals and 0.0 in vals and max(vals) >= 1.0"
// RUN: rm -f %t.tasks
// RUN: env SYMCC_TASK_GRAPH_OUT=%t.tasks %symcc -g -O0 %s -o %t
// RUN: python3 -c "rows=[l for l in open(r'%t.tasks') if l.startswith('#')]; assert any(l.startswith('#ENTRY ') for l in rows) and any(l.startswith('#SITE ') for l in rows) and any(l.startswith('#BRANCH ') for l in rows) and any(l.startswith('#E ') for l in rows)"

static int target(int x) {
  if (x == 7)
    return 1;
  return 0;
}

int main(int argc, char **argv) {
  (void)argv;
  if (argc == 2)
    return target(argc);
  if (argc == 3)
    return 3;
  return 0;
}
