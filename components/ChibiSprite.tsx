// components/ChibiSprite.tsx
// 可复用的Q版五条悟精灵组件，支持多种姿势 + 呼吸动画
// 使用方法：把你的Q版图片命名为 gojo_chibi.png 放到 assets/ 目录下

import React, { useEffect, useRef } from 'react';
import { Animated, Image, StyleSheet } from 'react-native';

const CHIBI = require('../assets/gojo_chibi.png');

interface Props {
  /** sit=坐着 | lie=趴着 | peek=探头 | tiny=小头像 */
  pose?: 'sit' | 'lie' | 'peek' | 'tiny';
  /** 基准尺寸，默认100 */
  size?: number;
}

export default function ChibiSprite({ pose = 'sit', size = 100 }: Props) {
  const anim = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(anim, { toValue: 1, duration: 1800, useNativeDriver: true }),
        Animated.timing(anim, { toValue: 0, duration: 1800, useNativeDriver: true }),
      ])
    );
    loop.start();
    return () => loop.stop();
  }, []);

  // 呼吸浮动幅度：趴着的时候小一些
  const translateY = anim.interpolate({
    inputRange: [0, 1],
    outputRange: [0, pose === 'lie' ? -2 : -5],
  });

  // 不同姿势的图片样式
  const imgStyle = (() => {
    switch (pose) {
      case 'sit':
        return { width: size, height: size, borderRadius: 10 };
      case 'lie':
        // 趴着：横向拉伸 + 旋转 + 纵向压扁
        return {
          width: size * 1.5,
          height: size * 0.65,
          borderRadius: 10,
          transform: [{ rotate: '12deg' }, { scaleY: 0.82 }],
        };
      case 'peek':
        // 探头：只显示上半部分（头 + 耳朵）
        return { width: size * 0.6, height: size * 0.5, borderRadius: 8 };
      case 'tiny':
        // 小圆头像
        return { width: 30, height: 30, borderRadius: 15 };
      default:
        return { width: size, height: size };
    }
  })();

  return (
    <Animated.View style={[s.wrap, { transform: [{ translateY }] }]}>
      <Image source={CHIBI} style={[s.img, imgStyle]} resizeMode="cover" />
    </Animated.View>
  );
}

const s = StyleSheet.create({
  wrap: { alignItems: 'center', justifyContent: 'center' },
  img:  {},
});