#include "../src/object_path_cache.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <iostream>

using namespace FF7RTelemetryExporter;
int main() {
    ObjectPathCache cache(2);
    auto now = std::chrono::steady_clock::now();
    ObjectIdentity current{7, 12, 0x100};
    bool live = true;
    int reads = 0;
    auto snapshot = [&](ObjectIdentity identity) -> std::optional<ObjectIdentity> {
        ++reads;
        assert(identity.index == current.index);
        return live ? std::optional{current} : std::nullopt;
    };
    cache.remember("/Game/World.Controller", current);
    for (int i = 0; i < 1000; ++i) {
        auto result = cache.resolve("/Game/World.Controller", now, snapshot);
        assert(result.address == 0x100 && !result.allow_search);
    }
    assert(reads == 1000);
    // Same index and address, different generation: reject BEFORE dereference.
    ++current.serial;
    auto result = cache.resolve("/Game/World.Controller", now, snapshot);
    assert(!result.address && result.allow_search);
    assert(!cache.resolve("/Game/World.Controller", now, snapshot).allow_search);
    cache.remember("/Game/World.Controller", current);
    assert(cache.resolve("/Game/World.Controller", now, snapshot).address == 0x100);
    live = false;
    assert(!cache.resolve("/Game/World.Controller", now, snapshot).address);
    assert(cache.resolve("/Game/World.Controller", now + std::chrono::seconds(2), snapshot).allow_search);
    // Address changes also invalidate a generation-zero identity.
    live = true;
    current.serial = 0;
    cache.remember("/Game/ZeroSerial", current);
    current.address = 0x200;
    assert(!cache.resolve("/Game/ZeroSerial", now, snapshot).address);
    UnversionedObjectRegistry zeros;
    zeros.remember(current);
    assert(zeros.current(current));
    zeros.retire(current.index);
    assert(!zeros.current(current));
    auto reused = current;
    reused.address = 0x300;
    zeros.remember(reused);
    assert(!zeros.current(current) && zeros.current(reused));
    zeros.clear();
    assert(!zeros.current(reused));
    // Missing paths are throttled; the finite cache evicts old identities.
    cache.resolve("/Game/Missing1", now, snapshot);
    cache.resolve("/Game/Missing2", now, snapshot);
    const auto before = reads;
    assert(cache.resolve("/Game/World.Controller", now, snapshot).allow_search);
    assert(reads == before);
    std::cout << "Object-path identity and bounded lookup regressions passed\n";
}
