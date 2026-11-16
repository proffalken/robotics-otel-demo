# ROS 2 Rolling demo nodes for the optional ros2ebpf stack.
# Rolling only: ros2ebpf refuses librmw_implementation.so from other distros.
FROM ros:rolling-ros-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ros-rolling-demo-nodes-cpp ros-rolling-demo-nodes-py \
    && rm -rf /var/lib/apt/lists/*

COPY robot.launch.xml /robot/robot.launch.xml

# The base image's entrypoint sources /opt/ros/rolling/setup.bash.
CMD ["ros2", "launch", "/robot/robot.launch.xml"]
