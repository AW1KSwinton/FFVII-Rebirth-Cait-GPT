#pragma once
#include <chrono>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <unordered_map>

namespace FF7RTelemetryExporter {
// Stores identity data, never a dereferenceable retained UObject.
struct ObjectIdentity {
    int32_t index{-1};
    int32_t serial{};
    uintptr_t address{};
    bool operator==(const ObjectIdentity&) const = default;
};

// For generation-zero objects, native deletion notifications are the lifetime
// authority. No UObject is dereferenced to check whether a record still lives.
class UnversionedObjectRegistry {
    std::unordered_map<int32_t, ObjectIdentity> objects;
public:
    void remember(ObjectIdentity identity) {
        if (!objects.contains(identity.index) && objects.size() >= 1024) objects.erase(objects.begin());
        objects[identity.index] = identity;
    }
    void retire(int32_t index) { objects.erase(index); }
    void clear() { objects.clear(); }
    std::optional<ObjectIdentity> current(ObjectIdentity expected) const {
        const auto found = objects.find(expected.index);
        if (found == objects.end() || found->second != expected) return {};
        return found->second;
    }
};

class ObjectPathCache {
    using Clock = std::chrono::steady_clock;
    struct Entry { ObjectIdentity identity; Clock::time_point retry{}; };
    std::unordered_map<std::string, Entry> entries;
    std::deque<std::string> insertion_order;
    size_t capacity;

    Entry& entry(const std::string& path) {
        if (auto found = entries.find(path); found != entries.end()) return found->second;
        if (entries.size() >= capacity) {
            entries.erase(insertion_order.front());
            insertion_order.pop_front();
        }
        insertion_order.push_back(path);
        return entries.emplace(path, Entry{}).first->second;
    }
public:
    explicit ObjectPathCache(size_t limit = 1024) : capacity(limit ? limit : 1) {}
    void remember(const std::string& path, ObjectIdentity identity) {
        entry(path).identity = identity;
    }
    void invalidate(const std::string& path) { entry(path).identity = {}; }
    struct Result { uintptr_t address{}; bool allow_search{}; };
    template<class Snapshot>
    Result resolve(const std::string& path, Clock::time_point now, Snapshot snapshot) {
        auto& cached = entry(path);
        if (cached.identity.index >= 0) {
            const auto current = snapshot(cached.identity);
            if (current && *current == cached.identity && current->address)
                return {current->address, false};
            cached.identity = {};
        }
        if (now < cached.retry) return {};
        cached.retry = now + std::chrono::seconds(2);
        return {0, true};
    }
};
}
