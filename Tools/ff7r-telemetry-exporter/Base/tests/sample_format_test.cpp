#include "../src/sample_format.hpp"
#include <cstdlib>
#include <iostream>

using namespace FF7RTelemetryExporter;

void check(bool value, const char* reason)
{
    if (!value) { std::cerr << reason << '\n'; std::exit(1); }
}

int main()
{
    SYSTEMTIME now{};
    now.wYear = 2026; now.wMonth = 9; now.wDay = 8; now.wHour = 14;
    const std::string legacy = "1|2026-09-08T14:00:00Z|0|1|0,10,20,5,10|10,20,200,0,90,0|EndGame||-1|0||0||1|test_pawn|0,90,0";
    const std::string physical = "10,20,110,40,90,100,200,0,1,1";
    Sample old{}, absent{}, present{};
    check(parse_complete_sample(old, legacy + "\n", now, 0), "legacy snapshot refused");
    check(parse_complete_sample(absent, legacy + "|\n", now, 0), "optional absent locomotion refused");
    check(absent.locomotion.empty(), "absent locomotion fabricated");
    check(parse_complete_sample(present, legacy + "|" + physical + "\n", now, 0), "complete Lua locomotion snapshot refused");
    check(present.locomotion.size() == 10 && present.locomotion[2] == 110 && present.locomotion[8] == 1,
          "measured feet or grounded state lost");
    for (const auto suffix : {"10,20,110,40,90,100,200,0,2,1", "10,20,nan,40,90,100,200,0,1,1",
                              "10,20,110,100,90,100,200,0,1,1", "10,20,110,40,90,100,200,0,1,1.5"})
    {
        Sample invalid{};
        check(!parse_complete_sample(invalid, legacy + "|" + suffix + "\n", now, 0), "invalid physical snapshot accepted");
    }
    Sample duplicate{}, incomplete{}, stale{}, excessive{};
    check(!parse_complete_sample(duplicate, legacy + "|" + physical + "\n", now, 1), "duplicate sequence accepted");
    check(!parse_complete_sample(incomplete, legacy + "|" + physical, now, 0), "partial write accepted");
    check(!parse_complete_sample(excessive, legacy + "|" + physical + "|extra\n", now, 0), "unrecognized extra field accepted");
    now.wMinute = 1;
    check(!parse_complete_sample(stale, legacy + "|" + physical + "\n", now, 0), "stale handoff accepted");
    std::cout << "PASS: native legacy/optional/physical snapshots, invalid geometry, duplicate/partial/stale rejection\n";
}
